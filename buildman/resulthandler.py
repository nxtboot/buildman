# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.
#
# Bloat-o-meter code used here Copyright 2004 Matt Mackall <mpm@selenic.com>
#

"""Result writer for buildman build results"""

import sys

from buildman.outcome import (BoardStatus, ErrLine, OUTCOME_OK,
                              OUTCOME_WARNING, OUTCOME_ERROR, OUTCOME_UNKNOWN)
from u_boot_pylib.terminal import tprint


class ResultHandler:
    """Handles display of build size results and summaries

    This class is responsible for displaying size information from builds,
    including per-architecture summaries, per-board details, and per-function
    bloat analysis.

    Attributes:
        col: terminal.Color object for coloured output
    """

    def __init__(self, col, opts):
        """Create a new ResultHandler

        Args:
            col: terminal.Color object for coloured output
            opts (DisplayOptions): Options controlling what to display
        """
        self._col = col
        self._opts = opts
        self._builder = None

    def set_builder(self, builder):
        """Set the builder for this result handler

        Args:
            builder (Builder): Builder object to use for getting results
        """
        self._builder = builder

    def colour_num(self, num):
        """Format a number with colour depending on its value

        Args:
            num (int): Number to format

        Returns:
            str: Formatted string (red if positive, green if negative/zero)
        """
        color = self._col.RED if num > 0 else self._col.GREEN
        if num == 0:
            return '0'
        return self._col.build(color, str(num))

    def print_func_size_detail(self, fname, old, new):
        """Print detailed size information for each function

        Args:
            fname (str): Filename to print (e.g. 'u-boot')
            old (dict): Dictionary of old function sizes, keyed by function name
            new (dict): Dictionary of new function sizes, keyed by function name
        """
        grow, shrink, add, remove, up, down = 0, 0, 0, 0, 0, 0
        delta, common = [], {}

        for a in old:
            if a in new:
                common[a] = 1

        for name in old:
            if name not in common:
                remove += 1
                down += old[name]
                delta.append([-old[name], name])

        for name in new:
            if name not in common:
                add += 1
                up += new[name]
                delta.append([new[name], name])

        for name in common:
            diff = new.get(name, 0) - old.get(name, 0)
            if diff > 0:
                grow, up = grow + 1, up + diff
            elif diff < 0:
                shrink, down = shrink + 1, down - diff
            delta.append([diff, name])

        delta.sort()
        delta.reverse()

        args = [add, -remove, grow, -shrink, up, -down, up - down]
        if max(args) == 0 and min(args) == 0:
            return
        args = [self.colour_num(x) for x in args]
        indent = ' ' * 15
        tprint(f'{indent}{self._col.build(self._col.YELLOW, fname)}: add: '
               f'{args[0]}/{args[1]}, grow: {args[2]}/{args[3]} bytes: '
               f'{args[4]}/{args[5]} ({args[6]})')
        tprint(f'{indent}  {"function":<38s} {"old":>7s} {"new":>7s} '
               f'{"delta":>7s}')
        for diff, name in delta:
            if diff:
                color = self._col.RED if diff > 0 else self._col.GREEN
                msg = (f'{indent}  {name:<38s} {old.get(name, "-"):>7} '
                       f'{new.get(name, "-"):>7} {diff:+7d}')
                tprint(msg, colour=color)

    def print_size_detail(self, target_list, base_board_dict, board_dict,
                          show_bloat):
        """Show detailed size information for each board

        Args:
            target_list (list): List of targets, each a dict containing:
                    'target': Target name
                    'total_diff': Total difference in bytes across all areas
                    <part_name>: Difference for that part
            base_board_dict (dict): Dict of base board outcomes
            board_dict (dict): Dict of current board outcomes
            show_bloat (bool): Show detail for each function
        """
        targets_by_diff = sorted(target_list, reverse=True,
        key=lambda x: x['_total_diff'])
        for result in targets_by_diff:
            printed_target = False
            for name in sorted(result):
                diff = result[name]
                if name.startswith('_'):
                    continue
                colour = self._col.RED if diff > 0 else self._col.GREEN
                msg = f' {name} {diff:+d}'
                if not printed_target:
                    tprint(f'{"":10s}  {result["_target"]:<15s}:',
                          newline=False)
                    printed_target = True
                tprint(msg, colour=colour, newline=False)
            if printed_target:
                tprint()
                if show_bloat:
                    target = result['_target']
                    outcome = board_dict[target]
                    base_outcome = base_board_dict[target]
                    for fname in outcome.func_sizes:
                        self.print_func_size_detail(fname,
                                                 base_outcome.func_sizes[fname],
                                                 outcome.func_sizes[fname])

    @staticmethod
    def calc_image_size_changes(target, sizes, base_sizes):
        """Calculate size changes for each image/part

        Args:
            target (str): Target board name
            sizes (dict): Dict of image sizes, keyed by image name
            base_sizes (dict): Dict of base image sizes, keyed by image name

        Returns:
            dict: Size changes, e.g.:
                {'_target': 'snapper9g45', 'data': 5, 'u-boot-spl:text': -4}
                meaning U-Boot data increased by 5 bytes, SPL text decreased
                by 4
        """
        err = {'_target' : target}
        for image in sizes:
            if image in base_sizes:
                base_image = base_sizes[image]
                # Loop through the text, data, bss parts
                for part in sorted(sizes[image]):
                    diff = sizes[image][part] - base_image[part]
                    if diff:
                        if image == 'u-boot':
                            name = part
                        else:
                            name = image + ':' + part
                        err[name] = diff
        return err

    def calc_size_changes(self, board_selected, board_dict, base_board_dict):
        """Calculate changes in size for different image parts

        The previous sizes are in Board.sizes, for each board

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            base_board_dict (dict): Dict of base board outcomes

        Returns:
            tuple: (arch_list, arch_count) where:
                arch_list: dict keyed by arch name, containing a list of
                    size-change dicts
                arch_count: dict keyed by arch name, containing the number of
                    boards for that arch
        """
        arch_list = {}
        arch_count = {}
        for target in board_dict:
            if target not in board_selected:
                continue
            base_sizes = base_board_dict[target].sizes
            outcome = board_dict[target]
            sizes = outcome.sizes
            err = self.calc_image_size_changes(target, sizes, base_sizes)
            arch = board_selected[target].arch
            if not arch in arch_count:
                arch_count[arch] = 1
            else:
                arch_count[arch] += 1
            if not sizes:
                pass    # Only add to our list when we have some stats
            elif not arch in arch_list:
                arch_list[arch] = [err]
            else:
                arch_list[arch].append(err)
        return arch_list, arch_count

    def print_size_summary(self, board_selected, board_dict, base_board_dict,
                           show_detail, show_bloat):
        """Print a summary of image sizes broken down by section.

        The summary takes the form of one line per architecture. The
        line contains deltas for each of the sections (+ means the section
        got bigger, - means smaller). The numbers are the average number
        of bytes that a board in this section increased by.

        For example:
           powerpc: (622 boards)   text -0.0
          arm: (285 boards)   text -0.0

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            base_board_dict (dict): Dict of base board outcomes
            show_detail (bool): Show size delta detail for each board
            show_bloat (bool): Show detail for each function
        """
        arch_list, arch_count = self.calc_size_changes(board_selected,
                                                       board_dict,
                                                       base_board_dict)

        # We now have a list of image size changes sorted by arch
        # Print out a summary of these
        for arch, target_list in arch_list.items():
            # Get total difference for each type
            totals = {}
            for result in target_list:
                total = 0
                for name, diff in result.items():
                    if name.startswith('_'):
                        continue
                    total += diff
                    if name in totals:
                        totals[name] += diff
                    else:
                        totals[name] = diff
                result['_total_diff'] = total

            self._print_arch_size_summary(arch, target_list, arch_count,
                                          totals, base_board_dict, board_dict,
                                          show_detail, show_bloat)

    def _print_arch_size_summary(self, arch, target_list, arch_count, totals,
                                 base_board_dict, board_dict,
                                 show_detail, show_bloat):
        """Print size summary for a single architecture

        Args:
            arch (str): Architecture name
            target_list (list): List of size-change dicts for this arch
            arch_count (dict): Dict of arch name to board count
            totals (dict): Dict of name to total size diff
            base_board_dict (dict): Dict of base board outcomes
            board_dict (dict): Dict of current board outcomes
            show_detail (bool): Show size delta detail for each board
            show_bloat (bool): Show detail for each function
        """
        count = len(target_list)
        printed_arch = False
        for name in sorted(totals):
            diff = totals[name]
            if diff:
                # Display the average difference in this name for this
                # architecture
                avg_diff = float(diff) / count
                color = self._col.RED if avg_diff > 0 else self._col.GREEN
                msg = f' {name} {avg_diff:+1.1f}'
                if not printed_arch:
                    tprint(f'{arch:>10s}: (for {count}/{arch_count[arch]} '
                           'boards)', newline=False)
                    printed_arch = True
                tprint(msg, colour=color, newline=False)

        if printed_arch:
            tprint()
            if show_detail:
                self.print_size_detail(target_list, base_board_dict, board_dict,
                                       show_bloat)

    def add_outcome(self, board_dict, arch_list, changes, char, color):
        """Add an output to our list of outcomes for each architecture

        This simple function adds failing boards (changes) to the
        relevant architecture string, so we can print the results out
        sorted by architecture.

        Args:
             board_dict (dict): Dict containing all boards
             arch_list (dict): Dict keyed by arch name. Value is a string
                 containing a list of board names which failed for that arch.
             changes (list): List of boards to add to arch_list
             char (str): Character to display for this board
             color (int): terminal.Colour object
        """
        done_arch = {}
        for target in changes:
            if target in board_dict:
                arch = board_dict[target].arch
            else:
                arch = 'unknown'
            text = self._col.build(color, ' ' + target)
            if arch not in done_arch:
                text = f' {self._col.build(color, char)}  {text}'
                done_arch[arch] = True
            if arch not in arch_list:
                arch_list[arch] = text
            else:
                arch_list[arch] += text

    def output_err_lines(self, err_lines, colour):
        """Output the line of error/warning lines, if not empty

        Args:
            err_lines: List of ErrLine objects, each an error or warning
                line, possibly including a list of boards with that
                error/warning
            colour: Colour to use for output

        Returns:
            int: 1 if any lines were output, 0 otherwise
        """
        if err_lines:
            out_list = []
            for line in err_lines:
                names = [brd.target for brd in line.brds]
                board_str = ' '.join(names) if names else ''
                if board_str:
                    out = self._col.build(colour, line.char + '(')
                    out += self._col.build(self._col.MAGENTA, board_str,
                                          bright=False)
                    out += self._col.build(colour, f') {line.errline}')
                else:
                    out = self._col.build(colour, line.char + line.errline)
                out_list.append(out)
            tprint('\n'.join(out_list))
            return 1
        return 0

    def display_arch_results(self, board_selected, brd_status, better_err,
                             worse_err, better_warn, worse_warn, show_unknown):
        """Display results by architecture

        Args:
            board_selected (dict): Dict containing boards to summarise
            brd_status (BoardStatus): Named tuple with board classifications
            better_err: List of ErrLine for fixed errors
            worse_err: List of ErrLine for new errors
            better_warn: List of ErrLine for fixed warnings
            worse_warn: List of ErrLine for new warnings
            show_unknown (bool): Whether to show unknown boards

        Returns:
            int: Number of error lines output
        """
        error_lines = 0
        if not any((brd_status.ok, brd_status.warn, brd_status.err,
                    brd_status.unknown, brd_status.new, worse_err, better_err,
                    worse_warn, better_warn)):
            return error_lines
        arch_list = {}
        self.add_outcome(board_selected, arch_list, brd_status.ok, '',
                         self._col.GREEN)
        self.add_outcome(board_selected, arch_list, brd_status.warn, 'w+',
                         self._col.YELLOW)
        self.add_outcome(board_selected, arch_list, brd_status.err, '+',
                         self._col.RED)
        self.add_outcome(board_selected, arch_list, brd_status.new, '*',
                         self._col.BLUE)
        if show_unknown:
            self.add_outcome(board_selected, arch_list, brd_status.unknown,
                             '?', self._col.MAGENTA)
        for arch, target_list in arch_list.items():
            tprint(f'{arch:>10s}: {target_list}')
            error_lines += 1
        error_lines += self.output_err_lines(better_err, colour=self._col.GREEN)
        error_lines += self.output_err_lines(worse_err, colour=self._col.RED)
        error_lines += self.output_err_lines(better_warn, colour=self._col.CYAN)
        error_lines += self.output_err_lines(worse_warn, colour=self._col.YELLOW)
        return error_lines

    @staticmethod
    def print_ide_output(board_selected, board_dict):
        """Print output for IDE mode

        Args:
            board_selected (dict): Dict of selected boards, keyed by target
            board_dict (dict): Dict of boards that were built, keyed by target
        """
        for target in board_dict:
            if target not in board_selected:
                continue
            outcome = board_dict[target]
            for line in outcome.err_lines:
                sys.stderr.write(line)

    @staticmethod
    def calc_config(delta, name, config):
        """Calculate configuration changes

        Args:
            delta: Type of the delta, e.g. '+'
            name: name of the file which changed (e.g. .config)
            config: configuration change dictionary
                key: config name
                value: config value
        Returns:
            String containing the configuration changes which can be
                printed
        """
        out = ''
        for key in sorted(config.keys()):
            out += f'{key}={config[key]} '
        return f'{delta} {name}: {out}'

    @classmethod
    def add_config(cls, lines, name, config_plus, config_minus, config_change):
        """Add changes in configuration to a list

        Args:
            lines: list to add to
            name: config file name
            config_plus: configurations added, dictionary
                key: config name
                value: config value
            config_minus: configurations removed, dictionary
                key: config name
                value: config value
            config_change: configurations changed, dictionary
                key: config name
                value: config value
        """
        if config_plus:
            lines.append(cls.calc_config('+', name, config_plus))
        if config_minus:
            lines.append(cls.calc_config('-', name, config_minus))
        if config_change:
            lines.append(cls.calc_config('c', name, config_change))

    def output_config_info(self, lines):
        """Output configuration change information

        Args:
            lines: List of configuration change strings
        """
        for line in lines:
            if not line:
                continue
            col = None
            if line[0] == '+':
                col = self.col.GREEN
            elif line[0] == '-':
                col = self.col.RED
            elif line[0] == 'c':
                col = self.col.YELLOW
            tprint('   ' + line, newline=True, colour=col)

    def show_environment_changes(self, board_selected, board_dict,
                                 environment, base_environment):
        """Show changes in environment variables

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            environment (dict): Dict of environment changes, keyed by
                board.target
            base_environment (dict): Dict of base environment, keyed by
                board.target
        """
        lines = []
        for target in board_dict:
            if target not in board_selected:
                continue

            tbase = base_environment[target]
            tenvironment = environment[target]
            environment_plus = {}
            environment_minus = {}
            environment_change = {}
            base = tbase.environment
            for key, value in tenvironment.environment.items():
                if key not in base:
                    environment_plus[key] = value
            for key, value in base.items():
                if key not in tenvironment.environment:
                    environment_minus[key] = value
            for key, value in base.items():
                new_value = tenvironment.environment.get(key)
                if new_value and value != new_value:
                    desc = f'{value} -> {new_value}'
                    environment_change[key] = desc

            self.add_config(lines, target, environment_plus,
                           environment_minus, environment_change)
        self.output_config_info(lines)

    def calc_config_changes(self, target, config, base_config, config_filenames,
                            arch, arch_config_plus, arch_config_minus,
                            arch_config_change):
        """Calculate configuration changes for a single target

        Args:
            target (str): Target board name
            config (dict): Dict of config changes, keyed by board.target
            base_config (dict): Dict of base config, keyed by board.target
            config_filenames (list): List of config filenames to check
            arch (str): Architecture name
            arch_config_plus (dict): Dict to update with added configs by arch
            arch_config_minus (dict): Dict to update with removed configs by
                arch
            arch_config_change (dict): Dict to update with changed configs by
                arch

        Returns:
            str: Summary of config changes for this target
        """
        all_config_plus = {}
        all_config_minus = {}
        all_config_change = {}
        tbase = base_config[target]
        tconfig = config[target]
        lines = []
        for name in config_filenames:
            if not tconfig.config[name]:
                continue
            config_plus = {}
            config_minus = {}
            config_change = {}
            base = tbase.config[name]
            for key, value in tconfig.config[name].items():
                if key not in base:
                    config_plus[key] = value
                    all_config_plus[key] = value
            for key, value in base.items():
                if key not in tconfig.config[name]:
                    config_minus[key] = value
                    all_config_minus[key] = value
            for key, value in base.items():
                new_value = tconfig.config[name].get(key)
                if new_value and value != new_value:
                    desc = f'{value} -> {new_value}'
                    config_change[key] = desc
                    all_config_change[key] = desc

            arch_config_plus[arch][name].update(config_plus)
            arch_config_minus[arch][name].update(config_minus)
            arch_config_change[arch][name].update(config_change)

            self.add_config(lines, name, config_plus, config_minus,
                           config_change)
        self.add_config(lines, 'all', all_config_plus,
                       all_config_minus, all_config_change)
        return '\n'.join(lines)

    def print_arch_config_summary(self, arch, arch_config_plus,
                                  arch_config_minus, arch_config_change,
                                  config_filenames):
        """Print configuration summary for a single architecture

        Args:
            arch (str): Architecture name
            arch_config_plus (dict): Dict of added configs by arch/filename
            arch_config_minus (dict): Dict of removed configs by arch/filename
            arch_config_change (dict): Dict of changed configs by arch/filename
            config_filenames (list): List of config filenames to check
        """
        lines = []
        all_plus = {}
        all_minus = {}
        all_change = {}
        for name in config_filenames:
            all_plus.update(arch_config_plus[arch][name])
            all_minus.update(arch_config_minus[arch][name])
            all_change.update(arch_config_change[arch][name])
            self.add_config(lines, name,
                           arch_config_plus[arch][name],
                           arch_config_minus[arch][name],
                           arch_config_change[arch][name])
        self.add_config(lines, 'all', all_plus, all_minus, all_change)
        if lines:
            tprint(f'{arch}:')
            self.output_config_info(lines)

    def show_config_changes(self, board_selected, board_dict, config,
                            base_config, config_filenames):
        """Show changes in configuration

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            config (dict): Dict of config changes, keyed by board.target
            base_config (dict): Dict of base config, keyed by board.target
            config_filenames (list): List of config filenames to check
        """
        summary = {}
        arch_config_plus = {}
        arch_config_minus = {}
        arch_config_change = {}
        arch_list = []

        for target in board_dict:
            if target not in board_selected:
                continue
            arch = board_selected[target].arch
            if arch not in arch_list:
                arch_list.append(arch)

        for arch in arch_list:
            arch_config_plus[arch] = {}
            arch_config_minus[arch] = {}
            arch_config_change[arch] = {}
            for name in config_filenames:
                arch_config_plus[arch][name] = {}
                arch_config_minus[arch][name] = {}
                arch_config_change[arch][name] = {}

        for target in board_dict:
            if target not in board_selected:
                continue
            arch = board_selected[target].arch
            summary[target] = self.calc_config_changes(
                target, config, base_config, config_filenames, arch,
                arch_config_plus, arch_config_minus, arch_config_change)

        lines_by_target = {}
        for target, lines in summary.items():
            if lines in lines_by_target:
                lines_by_target[lines].append(target)
            else:
                lines_by_target[lines] = [target]

        for arch in arch_list:
            self.print_arch_config_summary(arch, arch_config_plus,
                                          arch_config_minus,
                                          arch_config_change, config_filenames)

        for lines, targets in lines_by_target.items():
            if not lines:
                continue
            tprint(f"{' '.join(sorted(targets))} :")
            self.output_config_info(lines.split('\n'))

    @staticmethod
    def classify_boards(board_selected, board_dict, base_board_dict):
        """Classify boards into outcome categories

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            base_board_dict (dict): Dict of base board outcomes

        Returns:
            BoardStatus: Named tuple containing lists of board targets
        """
        ok = []      # List of boards fixed since last commit
        warn = []    # List of boards with warnings since last commit
        err = []     # List of new broken boards since last commit
        new = []     # List of boards that didn't exist last time
        unknown = [] # List of boards that were not built

        for target in board_dict:
            if target not in board_selected:
                continue

            # If the board was built last time, add its outcome to a list
            if target in base_board_dict:
                base_outcome = base_board_dict[target].rc
                outcome = board_dict[target]
                if outcome.rc == OUTCOME_UNKNOWN:
                    unknown.append(target)
                elif outcome.rc < base_outcome:
                    if outcome.rc == OUTCOME_WARNING:
                        warn.append(target)
                    else:
                        ok.append(target)
                elif outcome.rc > base_outcome:
                    if outcome.rc == OUTCOME_WARNING:
                        warn.append(target)
                    else:
                        err.append(target)
            else:
                new.append(target)
        return BoardStatus(ok, warn, err, new, unknown)

    @staticmethod
    def show_not_built(board_selected, board_dict):
        """Show boards that were not built

        This reports boards that couldn't be built due to toolchain issues.
        These have OUTCOME_UNKNOWN (no result file) or OUTCOME_ERROR with
        "Tool chain error" in the error lines.

        Args:
            board_selected (dict): Dict of selected boards, keyed by target
            board_dict (dict): Dict of boards that were built, keyed by target
        """
        not_built = []
        for target in board_selected:
            if target not in board_dict:
                not_built.append(target)
            else:
                outcome = board_dict[target]
                if outcome.rc == OUTCOME_UNKNOWN:
                    not_built.append(target)
                elif outcome.rc == OUTCOME_ERROR:
                    # Check for toolchain error in the error lines
                    for line in outcome.err_lines:
                        if 'Tool chain error' in line:
                            not_built.append(target)
                            break
        if not_built:
            tprint(f"Boards not built ({len(not_built)}): "
                   f"{', '.join(not_built)}")

    @staticmethod
    def _board_list(line, line_boards, list_error_boards):
        """Get a list of boards containing a particular error/warning line

        Args:
            line (str): Error line to search for
            line_boards (dict): Dict keyed by line, containing list of Board
                objects with that line
            list_error_boards (bool): True to return the board list, False to
                return empty list

        Returns:
            list: List of Board objects with that error line, or [] if
                list_error_boards is False
        """
        brds = []
        board_set = set()
        if list_error_boards:
            for brd in line_boards[line]:
                if brd not in board_set:
                    brds.append(brd)
                    board_set.add(brd)
        return brds

    @classmethod
    def calc_error_delta(cls, base_lines, base_line_boards, lines, line_boards,
                         char, list_error_boards):
        """Calculate the required output based on changes in errors

        Args:
            base_lines (list): List of errors/warnings for previous commit
            base_line_boards (dict): Dict keyed by error line, containing a
                list of the Board objects with that error in the previous
                commit
            lines (list): List of errors/warning for this commit, each a str
            line_boards (dict): Dict keyed by error line, containing a list
                of the Board objects with that error in this commit
            char (str): Character representing error ('') or warning ('w'). The
                broken ('+') or fixed ('-') characters are added in this
                function
            list_error_boards (bool): True to include board list in output

        Returns:
            tuple: (better_lines, worse_lines) where each is a list of
                ErrLine objects
        """
        better_lines = []
        worse_lines = []
        for line in lines:
            if line not in base_lines:
                errline = ErrLine(
                    char + '+',
                    cls._board_list(line, line_boards, list_error_boards),
                    line)
                worse_lines.append(errline)
        for line in base_lines:
            if line not in lines:
                errline = ErrLine(
                    char + '-',
                    cls._board_list(line, base_line_boards, list_error_boards),
                    line)
                better_lines.append(errline)
        return better_lines, worse_lines
