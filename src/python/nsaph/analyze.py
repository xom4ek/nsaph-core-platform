import argparse
import os

from nsaph.ds import create_datasource_def
from nsaph.reader import get_entries, get_readme
from nsaph.model import Table


def analyze(path: str, columns = None):
    entries, open_function = get_entries(path)
    table = Table(path, open_function)
    table.analyze(entries[0])
    if columns:
        for c in columns:
            x = c.split(':')
            table.add_column(x[0], x[1], int(x[2]))
    return table


if __name__ == '__main__':
    parser = argparse.ArgumentParser\
        (description="Analyze file or directory and prepare import")
    parser.add_argument("--source", "-s", help="Path to a file or directory",
                        required=True)
    parser.add_argument("--column", "-c", help="Additional columns", nargs="*")
    parser.add_argument("--outdir", help="Output directory")
    parser.add_argument("--readme", help="Readme.md file [optional]")

    args = parser.parse_args()

    table = analyze(args.source, args.column)

    if args.outdir:
        d = os.path.abspath(args.outdir)
    else:
        d = os.path.dirname(os.path.abspath(table.file_path))
        pd, cd = os.path.split(d)
        if cd == "data":
            d = os.path.join(pd, "config")
            if not os.path.isdir(d):
                os.mkdir(d)
    table.save(d)

    if args.readme:
        readme = args.readme
    else:
        readme = get_readme(table.file_path)
    create_datasource_def(table, readme, d)
