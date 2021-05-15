import logging
from typing import Optional

from nsaph_utils.utils.io_utils import as_dict

from nsaph.data_model.utils import basename, split
from nsaph.data_model.model import index_method, INDEX_NAME_PATTERN, INDEX_DDL_PATTERN


AUDIT_INSERT = """INSERT INTO {target} 
                ({columns}, REASON) 
                VALUES ({values}, '{reason}');"""

VALIDATION_PROC = """
CREATE OR REPLACE FUNCTION {schema}.validate_{source}() RETURNS TRIGGER AS ${schema}_{source}_validation$
-- Validate foreign key for {schema}.{source}
    BEGIN
        IF ({condition_pk}) THEN
            {action_pk}
            RETURN NULL;
        END IF;
        IF NOT EXISTS (
            SELECT FROM {parent_table}
            WHERE
                {condition_fk} 
        ) THEN
            {action_fk}
            RETURN NULL;
        END IF;
        IF EXISTS (
            SELECT FROM {schema}.{source}
            WHERE
                {condition_dup} 
        ) THEN
            {action_dup}
            RETURN NULL;
        END IF;
        RETURN NEW;
    END;
 
${schema}_{source}_validation$ LANGUAGE plpgsql;
"""

VALIDATION_TRIGGER = """
    CREATE TRIGGER {schema}_{name}_validation BEFORE INSERT ON {table}
        FOR EACH ROW EXECUTE FUNCTION {schema}.validate_{name}();
"""


class Domain:
    CREATE = "CREATE TABLE {name}"
    def __init__(self, spec, name):
        self.domain = name
        self.spec = as_dict(spec)
        if "schema" in self.spec[self.domain]:
            self.schema = self.spec[self.domain]["schema"]
        elif "schema" in self.spec:
            self.schema = self.spec["schema"]
        else:
            self.schema = None
        self.indices = []
        self.ddl = []
        self.conucrrent_indices = False
        index_policy = self.spec[self.domain].get("index")
        if index_policy is None or index_policy in ["selected"]:
            self.index_policy = "selected"
        elif index_policy in ["explicit"]:
            self.index_policy = "explicit"
        elif index_policy in ["all", "unless excluded"]:
            self.index_policy = "all"
        else:
            raise Exception("Invalid indexing policy: " + index_policy)

    def init(self) -> None:
        if self.schema:
            self.ddl = ["CREATE SCHEMA IF NOT EXISTS {};".format(self.schema)]
        else:
            self.ddl = []
        for s in self.spec[self.domain]:
            if s.startswith("schema."):
                self.ddl.append("CREATE SCHEMA IF NOT EXISTS {};".format(self.spec[self.domain][s]))
        tables = self.spec[self.domain]["tables"]
        nodes = {t: tables[t] for t in tables}
        for node in nodes:
            self.ddl_for_node((node, nodes[node]))
        return

    def fqn(self, table):
        if self.schema:
            return self.schema + '.' + table
        return table

    def find(self, table: str, root = None) -> Optional[dict]:
        if not root:
            tables = self.spec[self.domain]["tables"]
        else:
            tables = root["children"]
        if table in tables:
            return tables[table]
        for t in tables:
            d = self.find(table, tables[t])
            if d is not None:
                return d
        return None

    def find_dependent(self, table: str) -> list:
        t = self.find(table)
        if t is None:
            raise LookupError("Table {} does not exist in domain {}".format(table, self.domain))
        result = [self.fqn(table)]
        if "children" in t:
            for child in t["children"]:
                result.extend(self.find_dependent(child))
        t2 = self.spillover_table(table, t)
        if t2:
            result.append(t2)
        return result

    def drop(self, table, connection) -> list:
        tables = self.find_dependent(table)
        with connection.cursor() as cursor:
            for t in tables:
                sql = "DROP TABLE IF EXISTS {} CASCADE".format(t)
                logging.info(sql)
                cursor.execute(sql)
            if not connection.autocommit:
                connection.commit()
        return tables

    def spillover_table(self, table, definition):
        if "invalid.records" in definition:
            validation = definition["invalid.records"]
            action = validation["action"].lower()
            spec = self.spec[self.domain]
            if action == "insert":
                target = validation["target"]
                if "schema" in target:
                    ts = target["schema"]
                    if ts[0] == '$':
                        ts = spec[ts[1:]]
                else:
                    ts = spec["schema"]
                if "table" in target:
                    tt = target["table"]
                    if tt[0] == '$':
                        tt = spec[tt[1:]]
                else:
                    tt = table
                return "{}.{}".format(ts, tt)
        return None

    def ddl_for_node(self, node, parent = None) -> None:
        table_basename, definition = node
        columns = definition["columns"]
        cnames = {split(column)[0] for column in columns}
        features = []
        table = self.fqn(table_basename)
        fk = None
        ptable = None
        fk_columns = None
        if parent is not None:
            ptable, pdef = parent
            if "primary_key" not in pdef:
                raise Exception("Parent table {} must define primary key".format(ptable))
            fk_columns = pdef["primary_key"]
            fk_name = "{}_to_{}".format(table_basename, ptable)
            fk_column_list = ", ".join(fk_columns)
            fk = "CONSTRAINT {name} FOREIGN KEY ({columns}) REFERENCES {parent} ({columns})"\
                .format(name=fk_name, columns=fk_column_list, parent=self.fqn(ptable))
            for column in pdef["columns"]:
                c, _ = split(column)
                if c in fk_columns and c not in cnames:
                    columns.append(column)

        features.extend([self.column_spec(column) for column in columns])

        pk_columns = None
        if "primary_key" in definition:
            pk_columns = definition["primary_key"]
            pk = "PRIMARY KEY ({})".format(", ".join(pk_columns))
            features.append(pk)

        if fk:
            features.append(fk)

        create_table = (self.CREATE + " (\n\t{features}\n);").format(name=table, features=",\n\t".join(features))
        self.ddl.append(create_table)
        if "invalid.records" in definition:
            validation = definition["invalid.records"]
            action = validation["action"].lower()
            t2 = self.spillover_table(table_basename, definition)
            if t2:
                ff = [f for f in features if "CONSTRAINT" not in f and "PRIMARY KEY" not in f]
                ff.append("REASON VARCHAR(16)")
                ff.append("recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ")
                create_table = (self.CREATE + " (\n\t{features}\n);").format(name=t2, features=",\n\t".join(ff))
                self.ddl.append(create_table)
            self.add_fk_validation(table, pk_columns, action, t2, columns, ptable, fk_columns)


        for column in columns:
            if not self.need_index(column):
                continue
            self.indices.append(self.get_index_ddl(table, column))

        if "children" in definition:
            children = {t: definition["children"][t] for t in definition["children"]}
            for child in children:
                self.ddl_for_node((child, children[child]), parent=node)

    def need_index(self, column) -> bool:
        if self.index_policy == "all":
            return True
        n, c = split(column)
        if "index" in c:
            return True
        if self.index_policy == "selected":
            return index_method(n) is not None
        return False

    def get_index_ddl(self, table, column) -> str:
        if self.conucrrent_indices:
            option = "CONCURRENTLY"
        else:
            option = ""

        method = None
        iname = None
        if "index" in column:
            index = column["index"]
            if isinstance(index, str):
                iname = index
            else:
                if "name" in index:
                    iname = index["name"]
                if "using" in index:
                    method = index["using"]
        cname, column = split(column)
        if method:
            pass
        elif self.is_array(column):
            method = "GIN"
        else:
            method = "BTREE"
        if not iname:
            iname = INDEX_NAME_PATTERN.format(table = table.split('.')[-1], column = cname)
        return INDEX_DDL_PATTERN.format(
            option = option,
            name = iname,
            table = table,
            column = cname,
            method = method
        ) + ";"


    @staticmethod
    def is_array(column) -> bool:
        if "type" not in column:
            return False
        type = column["type"]
        return type.endswith("]")

    @staticmethod
    def is_generated(column):
        if not isinstance(column, dict):
            return False
        if "source" not in column:
            return False
        if not isinstance(column["source"], dict):
            return False
        if "type" not in column["source"]:
            return False
        return "generated" == column["source"]["type"].lower()

    def column_spec(self, column) -> str:
        name, column = split(column)
        t = column.get("type", "VARCHAR")
        if self.is_generated(column):
            if not "code" in column["source"]:
                raise Exception("Generated column must specify the compute code")
            code = column["source"]["code"]
            return "{} {} {}".format(name, t, code)
        return "{} {}".format(name, t)

    @classmethod
    def matches(cls, create_statement, list_of_tables) -> bool:
        create_statement = create_statement.strip()
        for t in list_of_tables:
            if create_statement.startswith(cls.CREATE.format(name=t)):
                return True
            for create in ["CREATE TRIGGER", "CREATE OR REPLACE FUNCTION"]:
                if create_statement.startswith(create) and t in create_statement:
                    return True

        return False

    def create(self, connection, list_of_tables = None):
        with connection.cursor() as cursor:
            if list_of_tables:
                statements = [
                    s for s in self.ddl if self.matches(s, list_of_tables)
                ]
            else:
                statements = self.ddl
            for statement in statements:
                logging.info(statement)
            sql = "\n".join(statements)
            cursor.execute(sql)
            if not connection.autocommit:
                connection.commit()
            logging.info("Schema and all tables for domain {} have been created".format(self.domain))

    def add_fk_validation(self, table, pk, action, target, columns, pt, fk_columns):
        if action == "insert":
            cc = []
            for c in columns:
                name, definition = split(c)
                if not self.is_generated(definition):
                    cc.append(name)
            vv = ["NEW.{}".format(c) for c in cc]
            actions = [
                AUDIT_INSERT.format(target=target, columns=','.join(cc), values=','.join(vv), reason=r)
                for r in ["DUPLICATE", "FOREIGN KEY", "PRIMARY KEY"]
            ]
        elif action == "ignore":
            actions = ["", "", ""]
        else:
            raise Exception("Invalid action on validation for table {}: {}".format(table, action))
        conditions = [
            "\n\t\t\t\tAND ".join(["NEW.{c} = {c}".format(c=c) for c in constraint])
            for constraint in [pk, fk_columns]
        ]
        conditions.append("\n\t\t\t\tOR ".join(["NEW.{c} IS NULL ".format(c=c) for c in pk]))
        # OR NEW.{c} = ''
        t = basename(table)

        sql = VALIDATION_PROC.format(schema=self.schema, source=t, parent_table=self.fqn(pt),
                                     condition_dup = conditions[0], action_dup = actions[0],
                                     condition_fk = conditions[1], action_fk = actions[1],
                                     condition_pk = conditions[2], action_pk = actions[2],
        )
        self.ddl.append(sql)
        sql = VALIDATION_TRIGGER.format(schema=self.schema, name=t, table=table).strip()
        self.ddl.append(sql)
