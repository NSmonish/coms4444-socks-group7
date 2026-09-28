from itertools import combinations

from models.player import GameContext, PlayerSnapshot, Selection, TurnContext
from models.player import Player as BasePlayer

PACK_COST = 10.0
SOCK_COST = PACK_COST / 6

# numbers we settled on after a lot of simulation runs
PARAMS = {
	# toss odd socks freely only if money left per day is at least this much
	# per roommate
	'spend_per_rm': 10 / 6 / 2,
	# roughly how many socks per roommate per day die from holes once the
	# drawer is worn in
	'hole_rate': 1 / 34,
	# penalty for wearing a worn out sock when we think we'll run dry
	'hole_weight': 1000.0,
	# extra socks to keep above the bare minimum
	'dry_margin': 4,
	# never toss socks younger than this many washes
	'protect_age': 2,
	# low budget stuff:
	# how quickly old sightings fade from our picture of the drawer (per day)
	'decay': 0.97,
	# money kept back for holes before anything counts as spare
	'low_reserve': 1.0,
	# only swap a sock out if a new one would find a free match this much
	# more often
	'gain_min': 0.1,
}


def age(shade: int) -> int:
	# rough wash count (white fades 255->127 by 2, black 0->64 by 1)
	return (255 - shade) // 2 if shade > 64 else shade


def worn_out(shade: int) -> bool:
	return shade == 127 or shade == 64


def mismatch(a: int, b: int) -> int:
	d = abs(a - b)
	return d if d > 6 else 0


class Player7(BasePlayer):
	def __init__(self, snapshot: PlayerSnapshot, ctx: GameContext) -> None:
		super().__init__(snapshot, ctx)
		self.days_seen = 0
		# day the money ran out - holes don't get replaced after that
		self.broke_since = None
		# shades we've been handed lately, as weighted counts
		self.seen = {'w': {}, 'b': {}}
		self.last_day = 0
		# spare money we can use on swaps, counted in socks
		self.credit = 0.0
		# living alone we can follow the whole drawer: shade -> how many.
		# holes are the only thing we can't see, so a worn out sock we wear
		# comes back counted as 0.75 of a sock
		self.solo = self.roommates == 1
		if self.solo:
			half = self.capacity // 2
			self.drawer = {255: float(half), 0: float(half)}
			self.pending = {'w': 0.0, 'b': 0.0}
			self.last_spent = 0.0
			self.last_hand = None
			self.touched = set()

	def will_run_dry(self, turn: TurnContext, left: float, days_left: int) -> bool:
		# rough guess - will holes from here on use up the spare socks plus
		# whatever the money left can still replace?
		p = PARAMS
		n = self.roommates
		lost = 0.0
		if self.broke_since is not None:
			lost = p['hole_rate'] * n * (turn.day - self.broke_since)
		spare = self.capacity - lost - self.selection_unit * n - p['dry_margin']
		holes = p['hole_rate'] * n * days_left
		return holes > spare + left / SOCK_COST

	def remember(self, offered: tuple[int, ...], day: int) -> None:
		fade = PARAMS['decay'] ** max(day - self.last_day, 1)
		self.last_day = day
		for hist in self.seen.values():
			for shade in list(hist):
				hist[shade] *= fade
				if hist[shade] < 0.01:
					del hist[shade]
		for shade in offered:
			hist = self.seen['w' if shade > 64 else 'b']
			hist[shade] = hist.get(shade, 0.0) + 1.0

	def free_match_chance(self, shade: int) -> float:
		# chance at least one of the other socks in a hand is within 6 shades
		# of this one, based on what we've been seeing
		if self.solo:
			white = shade > 64
			hist = {k: v for k, v in self.drawer.items() if (k > 64) == white}
			total = sum(self.drawer.values())
		else:
			hist = self.seen['w' if shade > 64 else 'b']
			total = sum(self.seen['w'].values()) + sum(self.seen['b'].values())
		if total == 0:
			return 1.0
		near = sum(w for s, w in hist.items() if abs(s - shade) <= 6) / total
		return 1.0 - (1.0 - near) ** (self.selection_unit - 1)

	def swap_gain(self, shade: int) -> float:
		# how much more often a brand new sock would find a free match
		fresh = 255 if shade > 64 else 0
		return self.free_match_chance(fresh) - self.free_match_chance(shade)

	def solo_update(self, offered: tuple[int, ...], turn: TurnContext) -> None:
		# put yesterday's socks back, count any packs bought, then take out today's hand
		if self.last_hand is not None:
			for shade, weight in self.last_hand:
				self.drawer[shade] = self.drawer.get(shade, 0.0) + weight
		packs = round((turn.total_spent - self.last_spent) / PACK_COST)
		self.last_spent = turn.total_spent
		for _ in range(packs):
			# living alone, a pack can only be for a colour we tossed or wore
			# out yesterday. if that's both, go by whose count is nearer 6
			active = [c for c in self.pending if c in self.touched] or list(self.pending)
			color = max(active, key=lambda c: self.pending[c])
			self.pending[color] = max(0.0, self.pending[color] - 6)
			fresh = 255 if color == 'w' else 0
			self.drawer[fresh] = self.drawer.get(fresh, 0.0) + 6
		for shade in offered:
			left = self.drawer.get(shade, 0.0) - 1
			if left > 0.01:
				self.drawer[shade] = left
			else:
				self.drawer.pop(shade, None)

	def solo_after(self, offered: tuple[int, ...], wear_idx, discard_idx) -> None:
		# remember what goes back tonight and what's waiting to be replaced
		back = []
		for i, shade in enumerate(offered):
			color = 'w' if shade > 64 else 'b'
			if i in wear_idx:
				washed = max(127, shade - 2) if shade > 64 else min(64, shade + 1)
				if worn_out(shade):
					back.append((washed, 0.75))
					self.pending[color] += 0.25
				else:
					back.append((washed, 1.0))
			elif i in discard_idx:
				self.pending[color] += 1
			else:
				back.append((shade, 1.0))
		self.last_hand = back
		# colours whose waiting-for-replacement count may have gone up today
		self.touched = {
			'w' if offered[i] > 64 else 'b'
			for i in range(len(offered))
			if i in discard_idx or (i in wear_idx and worn_out(offered[i]))
		}

	def select_socks(self, offered: tuple[int, ...], turn: TurnContext) -> Selection:
		n = len(offered)
		p = PARAMS
		self.remember(offered, turn.day)
		if self.solo:
			self.solo_update(offered, turn)

		days_left = max(self.days - turn.day + 1, 1)
		left = turn.budget_remaining
		if left == float('inf'):
			have_money, can_spend, short = True, True, False
		else:
			have_money = left >= PACK_COST
			if not have_money and self.broke_since is None:
				self.broke_since = turn.day
			can_spend = have_money and left / days_left >= p['spend_per_rm'] * self.roommates
			short = self.will_run_dry(turn, left if have_money else 0.0, days_left)
			# low budget - whatever we don't need for holes is spare. spread it
			# over the days left and split it between roommates so copies of
			# us in the same house don't each spend all of it
			holes = p['hole_rate'] * self.roommates * days_left * SOCK_COST
			spare = left - p['low_reserve'] * holes
			if spare > 0 and not short:
				self.credit = min(self.credit + spare / days_left / self.roommates / SOCK_COST, 3.0)

		# wear the closest pair, fresher ones if tied. if we think we'll run
		# dry, also stay off worn out socks since each wear can lose one
		hole_w = p['hole_weight'] if short else 0.0

		def cost(pair):
			a, b = offered[pair[0]], offered[pair[1]]
			return (mismatch(a, b) + hole_w * (worn_out(a) + worn_out(b)), age(a) + age(b))

		wear_idx = min(combinations(range(n), 2), key=cost)
		leftovers = [i for i in range(n) if i not in wear_idx]
		worn_shade = (offered[wear_idx[0]] + offered[wear_idx[1]]) / 2

		discard_idx = []
		for idx in leftovers:
			shade = offered[idx]
			if worn_out(shade):
				# worn out socks go while there's money to replace them, but
				# if we're about to run dry every sock counts so keep it
				if have_money and not short:
					discard_idx.append(idx)
			elif can_spend and shade != worn_shade and age(shade) >= p['protect_age']:
				# plenty of money - drop the odd ones out so the drawer stays tight
				discard_idx.append(idx)

		# tight money - use the small spare credit on the one sock that fits
		# in worst with the rest of the drawer
		if have_money and not can_spend and not short and self.credit >= 1.0:
			options = [
				i for i in leftovers if i not in discard_idx and age(offered[i]) >= p['protect_age']
			]
			if options:
				best = max(options, key=lambda i: self.swap_gain(offered[i]))
				if self.swap_gain(offered[best]) >= p['gain_min']:
					discard_idx.append(best)
					self.credit -= 1.0

		if self.solo:
			self.solo_after(offered, wear_idx, discard_idx)
		return Selection(wear=wear_idx, discard=tuple(discard_idx))
