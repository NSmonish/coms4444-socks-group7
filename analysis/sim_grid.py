"""Team 7's simulation grid, and a runner for measuring changes.

Every household includes team 7 (our player). X is each other team in turn
(1, 2, 3, 4, 6, 8, 9, 10), and the five-team mix is every choice of 4 others:

  Team 7 only                                 1 roommate    1 mix
  Team 7 + Team X                             2             8
  Team 7 + 4 other teams                      5            70
  9 * Team 7                                  9             1
  8 * Team 7 + Team X                         9             8
  18 * Team 7                                 18            1
  9 * Team 7 + 9 * Team X                     18            8
  36 * Team 7                                 36            1
  18 * Team 7 + 18 * Team X                   36            8

and each one is played with every combination of:

  Number of socks chosen   4, 5
  Sock drawer size         m * 4 * roommates + 12, m in 1, 2, 4, 10
  Budget                   0, 150, 300, 500, 1000, 1500, none
  Time in years            1, 2, 3, 5, 10 (360 days each)

Only combinations the engine allows are written (drawer size must be more
than socks chosen * roommates + 10).

  uv run analysis/sim_grid.py make

Run all of it (results land in results/sim_results.csv, one row per grid row
with team 7's and the household's average embarrassment beside it):

  uv run analysis/sim_grid.py run

Run it after each change. --swap puts a different player in team 7's place,
--compare checks the new results against an earlier run, and the other flags
cut the grid down to something that finishes quickly:

  uv run analysis/sim_grid.py run --max-roommates 9 --max-years 3 --out results/grid_7.csv
  uv run analysis/sim_grid.py run --swap 7=74 --max-roommates 9 --max-years 3 \\
    --out results/grid_74.csv --compare results/grid_7.csv

Each run replaces --out. Results are written one simulation at a time, so an
interrupted run picks up where it left off when started again with the same
--out and --resume.
"""

import argparse
import csv
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OTHERS = ['1', '2', '3', '4', '6', '8', '9', '10']
SOCKS_CHOSEN = [4, 5]
DRAWER_MULTS = [1, 2, 4, 10]
BUDGETS = ['0', '150', '300', '500', '1000', '1500', 'none']
YEARS = [1, 2, 3, 5, 10]
DAYS_PER_YEAR = 360
DEFAULT_SEED = 4444
GRID_CSV = ROOT / 'configs' / 'sim_grid.csv'

MIX = 'Roommates mix'
SOCKS = 'Number of socks chosen'
DRAWER = 'Sock drawer size'
ROOMMATES = 'Roommates'
BUDGET = 'Budget'
TIME = 'Time in years'
GRID_FIELDS = [MIX, SOCKS, DRAWER, ROOMMATES, BUDGET, TIME]
SCORE = 'Team 7 embarrassment per day'
AVERAGE = 'Average embarrassment per day'
RESULT_FIELDS = [
	*GRID_FIELDS,
	SCORE,
	AVERAGE,
	'Seed',
	'Team 7 player',
	'Money spent',
	'Sockless days',
	'Money ran out on day',
	'Seconds',
]
DEFAULT_RESULTS = ROOT / 'results' / 'sim_results.csv'


def mixes() -> list[tuple[str, str]]:
	"""(pattern, mix) for every household in the grid."""
	out = [('Team 7 only', 'Team 7 only')]
	out += [('Team 7 + Team X', f'Team 7 + Team {x}') for x in OTHERS]
	out += [
		('Team 7 + 4 other teams', ' + '.join(f'Team {t}' for t in ('7', *four)))
		for four in combinations(OTHERS, 4)
	]
	out.append(('9 * Team 7', '9 * Team 7'))
	out += [('8 * Team 7 + Team X', f'8 * Team 7 + Team {x}') for x in OTHERS]
	out.append(('18 * Team 7', '18 * Team 7'))
	out += [('9 * Team 7 + 9 * Team X', f'9 * Team 7 + 9 * Team {x}') for x in OTHERS]
	out.append(('36 * Team 7', '36 * Team 7'))
	out += [('18 * Team 7 + 18 * Team X', f'18 * Team 7 + 18 * Team {x}') for x in OTHERS]
	return out


def team_list(mix: str) -> list[str]:
	"""'8 * Team 7 + Team 1' -> ['7'] * 8 + ['1']."""
	teams = []
	for part in mix.removesuffix(' only').split(' + '):
		count, _, team = part.rpartition(' * ')
		teams += [team.removeprefix('Team ')] * int(count or 1)
	return teams


# ----------------------------------------------------------------------
# the grid
# ----------------------------------------------------------------------


def make(args: argparse.Namespace) -> None:
	rows, skipped = [], 0
	per_pattern: dict[str, list[int]] = {}
	for pattern, mix in mixes():
		n = len(team_list(mix))
		counts = per_pattern.setdefault(pattern, [0, 0])
		counts[0] += 1
		for socks in SOCKS_CHOSEN:
			for mult in DRAWER_MULTS:
				drawer = mult * 4 * n + 12
				if drawer <= socks * n + 10:
					skipped += len(BUDGETS) * len(YEARS)
					continue
				for budget in BUDGETS:
					for years in YEARS:
						rows.append(
							{
								MIX: mix,
								SOCKS: socks,
								DRAWER: drawer,
								ROOMMATES: n,
								BUDGET: budget,
								TIME: years,
							}
						)
						counts[1] += 1
	out = Path(args.out)
	out.parent.mkdir(parents=True, exist_ok=True)
	with out.open('w', newline='') as f:
		writer = csv.DictWriter(f, fieldnames=GRID_FIELDS)
		writer.writeheader()
		writer.writerows(rows)

	print(f'{len(rows) + skipped} combinations, {skipped} not allowed by the engine,')
	print(f'{len(rows)} written to {out}\n')
	print(f'  {"pattern":<30}{"mixes":>7}{"simulations":>13}')
	for pattern, (n_mixes, n_rows) in per_pattern.items():
		print(f'  {pattern:<30}{n_mixes:>7}{n_rows:>13}')
	print(f'  {"total":<30}{sum(c[0] for c in per_pattern.values()):>7}{len(rows):>13}')


# ----------------------------------------------------------------------
# running it
# ----------------------------------------------------------------------

_PLAYERS: dict | None = None


def play(job: dict) -> dict:
	global _PLAYERS
	from core.engine import Engine
	from core.registry import discover

	if _PLAYERS is None:
		_PLAYERS = discover()[0]
	us = job['Team 7 player']
	teams = [us if t == '7' else t for t in team_list(job[MIX])]
	random.seed(int(job['Seed']))
	start = time.time()
	engine = Engine(
		players=[_PLAYERS[t] for t in teams],
		capacity=int(job[DRAWER]),
		selection_unit=int(job[SOCKS]),
		days=int(job[TIME]) * DAYS_PER_YEAR,
		seed=int(job['Seed']),
		timeout=0,
		keep_records=False,
		budget=None if job[BUDGET] == 'none' else float(job[BUDGET]),
	)
	result = engine.run()
	scores = [p['mean_daily_embarrassment'] for p in result['players']]
	ours = [s for s, t, orig in zip(scores, teams, team_list(job[MIX]), strict=True) if orig == '7']
	return {
		**{k: job[k] for k in GRID_FIELDS},
		'Seed': job['Seed'],
		'Team 7 player': us,
		SCORE: statistics.fmean(ours),
		AVERAGE: statistics.fmean(scores),
		'Money spent': result['total_spent'],
		'Sockless days': result['total_sockless_days'],
		'Money ran out on day': result['budget_exhausted_on_day'] or '',
		'Seconds': round(time.time() - start, 2),
	}


def key(row: dict) -> tuple[str, ...]:
	return (*(str(row[k]) for k in GRID_FIELDS), str(row['Seed']))


def select(args: argparse.Namespace) -> list[dict]:
	with Path(args.grid).open(newline='') as f:
		rows = list(csv.DictReader(f))
	player = dict(s.split('=', 1) for s in args.swap).get('7', '7')
	budgets = {'none' if b.lower() in ('none', 'no limit') else b for b in args.budget or []}
	mixes = [m.lower() for m in args.mix or []]
	jobs = []
	for row in rows:
		mix = row[MIX]
		if args.max_roommates and int(row[ROOMMATES]) > args.max_roommates:
			continue
		if args.max_years and int(row[TIME]) > args.max_years:
			continue
		if args.socks and int(row[SOCKS]) != args.socks:
			continue
		if budgets and row[BUDGET] not in budgets:
			continue
		if mixes and not any(m in mix.lower() for m in mixes):
			continue
		jobs.append({**row, 'Seed': str(DEFAULT_SEED), 'Team 7 player': player})
	if args.sample and args.sample < len(jobs):
		jobs = random.Random(DEFAULT_SEED).sample(jobs, args.sample)
	if args.limit:
		jobs = jobs[: args.limit]
	return jobs


def load_results(path: Path) -> dict[tuple[str, ...], dict]:
	if not path.exists():
		return {}
	with path.open(newline='') as f:
		return {key(r): r for r in csv.DictReader(f)}


def run(args: argparse.Namespace) -> None:
	jobs = select(args)
	out = Path(args.out)
	out.parent.mkdir(parents=True, exist_ok=True)
	done = load_results(out) if args.resume else {}
	todo = [j for j in jobs if key(j) not in done]
	print(
		f'{len(jobs)} simulations selected, {len(jobs) - len(todo)} already in {out}, running {len(todo)}'
	)

	fresh = not args.resume or not out.exists()
	with out.open('a' if args.resume else 'w', newline='') as f:
		writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
		if fresh:
			writer.writeheader()
		# smallest households first: 1 roommate and all its combinations, then 2, 5, ...
		todo.sort(key=lambda j: int(j[ROOMMATES]))
		start = time.time()
		with ProcessPoolExecutor(max_workers=args.workers) as pool:
			futures = [pool.submit(play, job) for job in todo]
			for finished, future in enumerate(as_completed(futures), 1):
				row = future.result()
				writer.writerow(row)
				f.flush()
				if finished % 25 == 0 or finished == len(todo):
					elapsed = time.time() - start
					print(
						f'  {finished}/{len(todo)}  {elapsed / 60:.1f} min  '
						f'(on {row[ROOMMATES]} roommates)',
						flush=True,
					)

	put_in_grid_order(out, args.grid)
	wanted = {key(j) for j in jobs}
	summarise({k: v for k, v in load_results(out).items() if k in wanted}, args)


def put_in_grid_order(out: Path, grid: str) -> None:
	with Path(grid).open(newline='') as f:
		order = {tuple(r[k] for k in GRID_FIELDS): i for i, r in enumerate(csv.DictReader(f))}
	with out.open(newline='') as f:
		rows = list(csv.DictReader(f))
	rows.sort(key=lambda r: (order.get(tuple(r[k] for k in GRID_FIELDS), len(order)), r['Seed']))
	with out.open('w', newline='') as f:
		writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
		writer.writeheader()
		writer.writerows(rows)


def summarise(results: dict[tuple[str, ...], dict], args: argparse.Namespace) -> None:
	if not results:
		print('no results')
		return
	scores = [float(r[SCORE]) for r in results.values()]
	sockless = sum(1 for r in results.values() if float(r['Sockless days']) > 0)
	print(
		f'\nteam 7 played by: {next(iter(results.values()))["Team 7 player"]}  ({len(scores)} simulations)'
	)
	print(f'  mean daily embarrassment   {statistics.fmean(scores):.4f}')
	print(f'  median                     {statistics.median(scores):.4f}')
	print(f'  simulations with sockless  {sockless}')

	if not args.compare:
		return
	other = load_results(Path(args.compare))
	pairs = [(float(r[SCORE]), float(other[k][SCORE]), r) for k, r in results.items() if k in other]
	if not pairs:
		print(f'\nnothing in common with {args.compare}')
		return
	mine = statistics.fmean(p[0] for p in pairs)
	theirs = statistics.fmean(p[1] for p in pairs)
	better = sum(1 for a, b, _ in pairs if a < b - 1e-9)
	worse = sum(1 for a, b, _ in pairs if a > b + 1e-9)
	print(f'\nagainst {args.compare} on {len(pairs)} shared simulations:')
	print(f'  this run {mine:.4f}   other {theirs:.4f}   (lower is better)')
	print(f'  better in {better}, worse in {worse}, tied in {len(pairs) - better - worse}')
	losses = sorted((p for p in pairs if p[0] > p[1] + 1e-9), key=lambda p: p[1] - p[0])[:10]
	if losses:
		print('  biggest losses:')
		for a, b, r in losses:
			print(
				f'    {r[MIX]}, {r[SOCKS]} socks, drawer {r[DRAWER]}, budget {r[BUDGET]}, '
				f'{r[TIME]} years:  {a:.3f} vs {b:.3f}'
			)


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	sub = parser.add_subparsers(dest='command', required=True)

	p_make = sub.add_parser('make', help='write every runnable combination to a csv')
	p_make.add_argument('--out', default=str(GRID_CSV))

	p_run = sub.add_parser('run', help='play rows from the grid csv')
	p_run.add_argument('--grid', default=str(GRID_CSV))
	p_run.add_argument(
		'--out', default=str(DEFAULT_RESULTS), help='results csv (replaced on each run)'
	)
	p_run.add_argument(
		'--resume',
		action='store_true',
		help='append to --out and skip simulations already in it instead of replacing it',
	)
	p_run.add_argument(
		'--swap', action='append', default=[], help="7=CODE plays team 7's place with CODE"
	)
	p_run.add_argument('--max-roommates', type=int)
	p_run.add_argument('--max-years', type=int)
	p_run.add_argument(
		'--socks', type=int, choices=SOCKS_CHOSEN, help='only this number of socks chosen'
	)
	p_run.add_argument('--budget', nargs='+', help='only these budgets, e.g. 500 1000 none')
	p_run.add_argument(
		'--mix', nargs='+', help="only mixes containing this text, e.g. 'Team 3' or '18 *'"
	)
	p_run.add_argument('--sample', type=int, help='random subset of this many simulations')
	p_run.add_argument('--limit', type=int)
	p_run.add_argument('--workers', type=int, default=os.cpu_count())
	p_run.add_argument('--compare', help='earlier results csv to compare against')

	args = parser.parse_args()
	make(args) if args.command == 'make' else run(args)


if __name__ == '__main__':
	main()
