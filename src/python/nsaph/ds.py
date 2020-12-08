import os

import yaml

from nsaph.md import parse
from nsaph.model import Table


def create(table: Table, readme: str = None):
    t = {
        "table_name": table.table,
        "columns": []
    }
    ds = {
        "databases": [
            {
                "database_name": "NSAPH",
                "tables": [
                    t
                ]

            }
        ]
    }

    if readme:
        desc, cds = parse(readme, table.csv_columns)
        t["description"] = desc
    else:
        cds = {}

    for i in range(0, len(table.sql_columns)):
        c = table.sql_columns[i]
        type = table.types[i]
        c1 = table.csv_columns[i]
        if c1 in cds:
            desc = cds[c1]
        elif c != c1:
            desc = "'{}'".format(c1)
        else:
            desc = None
        column = {
            "column_name": c,
            "type": type,
            "description": desc
        }
        t["columns"].append(column)
    return ds


def write(ds, out_path: str):
    with open (out_path, "w") as o:
        yaml.dump(ds, o, sort_keys=True)


def create_datasource_def(t: Table, readme: str, out_dir: str):
    ds = create(t, readme)
    out_path = os.path.join(out_dir, t.table + ".yml")
    write(ds, out_path)
