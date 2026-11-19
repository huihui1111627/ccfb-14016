#!/usr/bin/env python3
"""跑全部验收剧本。"""

import sys

from acceptance_scenarios import all_scenarios
from trunking.scenario import run_scenario


def main():
    total_fail = 0
    for sc in all_scenarios():
        _, failures = run_scenario(sc)
        if failures:
            total_fail += len(failures)
            print("✗ %s" % sc.name)
            for f in failures:
                print("    %s" % f)
        else:
            print("✓ %s" % sc.name)
    print("-" * 50)
    if total_fail:
        print("失败 %d 项" % total_fail)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
