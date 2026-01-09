# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.
#
# Bloat-o-meter code used here Copyright 2004 Matt Mackall <mpm@selenic.com>
#

"""Result writer for buildman build results"""

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
