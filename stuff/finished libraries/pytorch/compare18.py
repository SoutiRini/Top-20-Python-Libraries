"""Display class to aggregate and print the results of many measurements."""
import collections
import itertools as it
from typing import List, Optional, Tuple

import numpy as np

import utils.common as common

__all__ = ["Compare"]

BEST = "\033[92m"
GOOD = "\033[34m"
BAD = "\033[2m\033[91m"
VERY_BAD = "\033[31m"
BOLD = "\033[1m"
TERMINATE = "\033[0m"


# Classes to separate internal bookkeeping from what is rendered.
class _Column(object):
    def __init__(
        self,
        grouped_results: List[List[common.Measurement]],
        time_scale: float,
        time_unit: str,
        trim_significant_figures: bool,
        highlight_warnings: bool,
    ):
        self._grouped_results = grouped_results
        self._flat_results = list(it.chain(*grouped_results))
        self._time_scale = time_scale
        self._time_unit = time_unit
        self._trim_significant_figures = trim_significant_figures
        self._highlight_warnings = highlight_warnings and any(r.has_warnings for r in self._flat_results)
        leading_digits = [
            int(np.ceil(np.log10(r.median / self._time_scale)))
            for r in self._flat_results
        ]
        unit_digits = max(leading_digits)
        decimal_digits = min(
            max(m.significant_figures - digits, 0)
            for digits, m in zip(leading_digits, self._flat_results)
        ) if self._trim_significant_figures else 1
        length = unit_digits + decimal_digits + (1 if decimal_digits else 0)
        self._template = f"{{:>{length}.{decimal_digits}f}}{{:>{7 if self._highlight_warnings else 0}}}"

    def get_results_for(self, group):
        return self._grouped_results[group]

    def num_to_str(self, value: float, estimated_sigfigs: int, spread: Optional[float]):
        if self._trim_significant_figures:
            value = common.trim_sigfig(value, estimated_sigfigs)
        return self._template.format(
            value,
            f" (! {spread:.0f}%)" if self._highlight_warnings and spread is not None else "")


class _Row(object):
    def __init__(self, results, row_group, render_env, env_str_len,
                 row_name_str_len, time_scale, colorize, num_threads=None):
        super(_Row, self).__init__()
        self._results = results
        self._row_group = row_group
        self._render_env = render_env
        self._env_str_len = env_str_len
        self._row_name_str_len = row_name_str_len
        self._time_scale = time_scale
        self._colorize = colorize
        self._columns = None
        self._num_threads = num_threads

    def register_columns(self, columns: Tuple[_Column]):
        self._columns = columns

    def as_column_strings(self):
        env = f"({self._results[0].env})" if self._render_env else ""
        env = env.ljust(self._env_str_len + 4)
        output = ["  " + env + self._results[0].as_row_name]
        for m, col in zip(self._results, self._columns):
            output.append(col.num_to_str(
                m.median / self._time_scale,
                m.significant_figures,
                m.median / m._iqr if m.has_warnings else None
            ))
        return output

    @staticmethod
    def color_segment(segment, value, group_values):
        best_value = min(group_values)
        if value <= best_value * 1.01 or value <= best_value + 100e-9:
            return BEST + BOLD + segment + TERMINATE * 2
        if value <= best_value * 1.1:
            return GOOD + BOLD + segment + TERMINATE * 2
        if value >= best_value * 5:
            return VERY_BAD + BOLD + segment + TERMINATE * 2
        if value >= best_value * 2:
            return BAD + segment + TERMINATE * 2

        return segment

    def row_separator(self, overall_width):
        return (
            [f"{self._num_threads} threads: ".ljust(overall_width, "-")]
            if self._num_threads is not None else []
        )

    def finalize_column_strings(self, column_strings, col_widths):
        row_contents = [column_strings[0].ljust(col_widths[0])]
        for col_str, width, result, column in zip(column_strings[1:], col_widths[1:], self._results, self._columns):
            col_str = col_str.center(width)
            if self._colorize:
                group_medians = [r.median for r in column.get_results_for(self._row_group)]
                col_str = self.color_segment(col_str, result.median, group_medians)
            row_contents.append(col_str)
        return row_contents


class Table(object):
    def __init__(self, results: List[common.Measurement], colorize: bool,
                 trim_significant_figures: bool, highlight_warnings: bool):
        assert len(set(r.label for r in results)) == 1

        self.results = results
        self._colorize = colorize
        self._trim_significant_figures = trim_significant_figures
        self._highlight_warnings = highlight_warnings
        self.label = results[0].label
        self.time_unit, self.time_scale = common.select_unit(
            min(r.median for r in results)
        )

        self.row_keys = common.ordered_unique([self.row_fn(i) for i in results])
        self.row_keys.sort(key=lambda args: args[:2])  # preserve stmt order
        self.column_keys = common.ordered_unique([self.col_fn(i) for i in results])
        self.rows, self.columns = self.populate_rows_and_columns()

    @staticmethod
    def row_fn(m: common.Measurement):
        return m.num_threads, m.env, m.as_row_name

    @staticmethod
    def col_fn(m: common.Measurement):
        return m.description

    def populate_rows_and_columns(self):
        rows, columns = [], []

        ordered_results = [[None for _ in self.column_keys] for _ in self.row_keys]
        row_position = {key: i for i, key in enumerate(self.row_keys)}
        col_position = {key: i for i, key in enumerate(self.column_keys)}
        for r in self.results:
            i = row_position[self.row_fn(r)]
            j = col_position[self.col_fn(r)]
            ordered_results[i][j] = r

        unique_envs = {r.env for r in self.results}
        render_env = len(unique_envs) > 1
        env_str_len = max(len(i) for i in unique_envs) if render_env else 0

        row_name_str_len = max(len(r.as_row_name) for r in self.results)

        prior_num_threads = -1
        prior_env = ""
        row_group = -1
        rows_by_group = []
        for (num_threads, env, _), row in zip(self.row_keys, ordered_results):
            thread_transition = (num_threads != prior_num_threads)
            if thread_transition:
                prior_num_threads = num_threads
                prior_env = ""
                row_group += 1
                rows_by_group.append([])
            rows.append(
                _Row(
                    results=row,
                    row_group=row_group,
                    render_env=(render_env and env != prior_env),
                    env_str_len=env_str_len,
                    row_name_str_len=row_name_str_len,
                    time_scale=self.time_scale,
                    colorize=self._colorize,
                    num_threads=num_threads if thread_transition else None,
                )
            )
            rows_by_group[-1].append(row)
            prior_env = env

        for i in range(len(self.column_keys)):
            grouped_results = [tuple(row[i] for row in g) for g in rows_by_group]
            column = _Column(
                grouped_results=grouped_results, time_scale=self.time_scale,
                time_unit=self.time_unit,
                trim_significant_figures=self._trim_significant_figures,
                highlight_warnings=self._highlight_warnings,)
            columns.append(column)

        rows, columns = tuple(rows), tuple(columns)
        for r in rows:
            r.register_columns(columns)
        return rows, columns

    def render(self):
        string_rows = [[""] + self.column_keys]
        for r in self.rows:
            string_rows.append(r.as_column_strings())
        num_cols = max(len(i) for i in string_rows)
        for r in string_rows:
            r.extend(["" for _ in range(num_cols - len(r))])

        col_widths = [max(len(j) for j in i) for i in zip(*string_rows)]
        finalized_columns = ["  |  ".join(i.center(w) for i, w in zip(string_rows[0], col_widths))]
        overall_width = len(finalized_columns[0])
        for string_row, row in zip(string_rows[1:], self.rows):
            finalized_columns.extend(row.row_separator(overall_width))
            finalized_columns.append("  |  ".join(row.finalize_column_strings(string_row, col_widths)))
        print("[" + (" " + self.label + " ").center(overall_width - 2, "-") + "]")
        print("\n".join(finalized_columns))
        print(f"\nTimes are in {common.unit_to_english(self.time_unit)}s ({self.time_unit}).")
        if self._highlight_warnings and any(r.has_warnings for r in self.results):
            print("(! XX%) Measurement has high variance, where XX is the median / IQR * 100.")
        print("\n")


class Compare(object):
    def __init__(self, results: List[common.Measurement]):
        self._results = []
        self.extend_results(results)
        self._trim_significant_figures = False
        self._colorize = False
        self._highlight_warnings = False

    def extend_results(self, results):
        for r in results:
            if not isinstance(r, common.Measurement):
                raise ValueError(
                    "Expected an instance of `Measurement`, " f"got {type(r)} instead."
                )
        self._results.extend(results)

    def trim_significant_figures(self):
        self._trim_significant_figures = True

    def colorize(self):
        self._colorize = True

    def highlight_warnings(self):
        self._highlight_warnings = True

    def print(self):
        self._render()

    def _render(self):
        results = common.merge_measurements(self._results)
        results = self._group_by_label(results)
        for group in results.values():
            self._layout(group)

    def _group_by_label(self, results):
        grouped_results = collections.defaultdict(list)
        for r in results:
            grouped_results[r.label].append(r)
        return grouped_results

    def _layout(self, results: List[common.Measurement]):
        table = Table(results, self._colorize, self._trim_significant_figures,
                      self._highlight_warnings)
        table.render()
