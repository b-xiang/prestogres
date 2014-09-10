import plpy
import presto_client
from collections import namedtuple
import time

# Maximum length for identifiers (e.g. table names, column names, function names)
# defined in pg_config_manual.h
PG_NAMEDATALEN = 64

# convert Presto query result field types to PostgreSQL types
def _pg_result_type(presto_type):
    if presto_type == "varchar":
        return "varchar(255)"
    if presto_type == "varbinary":
        return "bytea"
    elif presto_type == "double":
        return "double precision"
    else:
        # assuming Presto and PostgreSQL use the same SQL standard name
        return presto_type

# convert Presto table column types to PostgreSQL types
def _pg_table_type(presto_type):
    if presto_type == "varchar":
        return "varchar(255)"
    if presto_type == "varbinary":
        return "bytea"
    elif presto_type == "double":
        return "double precision"
    else:
        # assuming Presto and PostgreSQL use the same SQL standard name
        return presto_type

# queries can include same column name twice but tables can't.
def _rename_duplicated_column_names(column_names):
    renamed = []
    used_names = set()
    for original_name in column_names:
        name = original_name
        while name in used_names:
            name += "_"
        if name != original_name:
            plpy.warning("Result column %s is renamed to %s because the name appears twice in a query result" % \
                    (plpy.quote_ident(original_name), plpy.quote_ident(name)))
        used_names.add(name)
        renamed.append(name)
    return renamed

# build CREATE TEMPORARY TABLE statement
def _build_create_temp_table_sql(table_name, column_names, column_types):
    create_sql = ["create temporary table %s (\n  " % plpy.quote_ident(table_name)]

    first = True
    for column_name, column_type in zip(column_names, column_types):
        if first:
            first = False
        else:
            create_sql.append(",\n  ")

        create_sql.append(plpy.quote_ident(column_name))
        create_sql.append(" ")
        create_sql.append(column_type)

    create_sql.append("\n)")
    return ''.join(create_sql)

# build CREATE TABLE statement
def _build_alter_table_holder_sql(schema_name, table_name, column_names, column_types, not_nulls):
    alter_sql = ["alter table %s.%s \n  " % (plpy.quote_ident(schema_name), plpy.quote_ident(table_name))]

    first = True
    for column_name, column_type, not_null in zip(column_names, column_types, not_nulls):
        if first:
            first = False
        else:
            alter_sql.append(",\n  ")

        alter_sql.append("add %s %s" % (plpy.quote_ident(column_name), column_type))

        if not_null:
            alter_sql.append(" not null")

    return ''.join(alter_sql)

# build INSERT INTO statement and string format to build VALUES (..), ...
def _build_insert_into_sql(table_name, column_names):
    # INSERT INTO table_name (column_name, column_name, ...)
    insert_sql = ["insert into %s (\n  " % plpy.quote_ident(table_name)]

    first = True
    for column_name in column_names:
        if first:
            first = False
        else:
            insert_sql.append(",\n  ")

        insert_sql.append(plpy.quote_ident(column_name))

    insert_sql.append("\n) values\n")

    return ''.join(insert_sql)

# create a prepared statement for batch INSERT
def _plan_batch(insert_sql, column_types, batch_size):
    index = 1  # PostgreSQL's place holder begins from 1

    # append value list (...), (...), ... at the end of insert_sql
    batch_isnert_sql = [insert_sql]
    first = True
    for i in range(batch_size):
        if first:
            first = False
        else:
            batch_isnert_sql.append(", ")

        # ($1::column_type, $2::column_type, ...)
        batch_isnert_sql.append("(")
        value_first = True
        for column_type in column_types:
            if value_first:
                value_first = False
            else:
                batch_isnert_sql.append(", ")

            batch_isnert_sql.append("$%s::%s" % (index, column_type))
            index += 1
        batch_isnert_sql.append(")")

    return plpy.prepare(''.join(batch_isnert_sql), column_types * batch_size)

# run batch INSERT
def _batch_insert(insert_sql, batch_size, column_types, rows):
    full_batch_plan = None

    batch = []
    for row in rows:
        batch.append(row)
        batch_len = len(batch)
        if batch_len >= batch_size:
            if full_batch_plan is None:
                full_batch_plan = _plan_batch(insert_sql, column_types, batch_len)
            plpy.execute(full_batch_plan, [item for sublist in batch for item in sublist])
            del batch[:]

    if batch:
        plan = _plan_batch(insert_sql, column_types, len(batch))
        plpy.execute(plan, [item for sublist in batch for item in sublist])

class SchemaCache(object):
    def __init__(self):
        self.server = None
        self.user = None
        self.catalog = None
        self.schema = None
        self.schema_names = None
        self.statements = None
        self.expire_time = None
        self.query_cache = {}

    def is_cached(self, server, user, catalog, schema, current_time):
        return self.server == server and self.user == user \
               and self.catalog == catalog and self.schema == schema \
               and self.statements is not None and current_time < self.expire_time

    def set_cache(self, server, user, catalog, schema, schema_names, statements, expire_time):
        self.server = server
        self.user = user
        self.catalog = catalog
        self.schema = schema
        self.schema_names = schema_names
        self.statements = statements
        self.expire_time = expire_time
        self.query_cache = {}

def _get_session_time_zone():
    rows = plpy.execute("show timezone")
    return rows[0].values()[0]

def _get_session_search_path_array():
    rows = plpy.execute("select ('{' || current_setting('search_path') || '}')::text[]")
    return rows[0].values()[0]

QueryResult = namedtuple("QueryResult", ("column_names", "column_types", "result"))

OidToTypeNameMapping = {}

# information_schema on PostgreSQL have some special column types where
# CREATE TABLE can't use the types (although SELECT returns the types).
def _pg_convert_infoschema_type(infoschema_type):
    if infoschema_type == "sql_identifier":
        return "name"
    if infoschema_type == "cardinal_number":
        return "int"
    elif infoschema_type == "character_data":
        return "name"
    elif infoschema_type == "yes_or_no":
        return "text"
    else:
        return infoschema_type

def _load_oid_to_type_name_mapping(oids):
    oids = filter(lambda oid: oid not in OidToTypeNameMapping, oids)
    if oids:
        sql = ("select oid, typname" \
               " from pg_catalog.pg_type" \
               " where oid in (%s)") % (", ".join(map(str, oids)))
        for row in plpy.execute(sql):
            type_name = _pg_convert_infoschema_type(row["typname"])
            OidToTypeNameMapping[int(row["oid"])] = type_name

    return OidToTypeNameMapping

Column = namedtuple("Column", ("name", "type", "nullable"))

SchemaCacheEntry = SchemaCache()

def run_presto_as_temp_table(server, user, catalog, schema, result_table, query):
    try:
        search_path = _get_session_search_path_array()
        if search_path != ['$user', 'public'] and len(search_path) > 0:
            # search_path is changed explicitly. Use the first schema
            schema = search_path[0]

        client = presto_client.Client(server=server, user=user, catalog=catalog, schema=schema, time_zone=_get_session_time_zone())

        create_sql = "create temporary table %s (\n  " % plpy.quote_ident(result_table)
        insert_sql = "insert into %s (\n  " % plpy.quote_ident(result_table)
        values_types = []

        q = client.query(query)
        try:
            # result schema
            column_names = []
            column_types = []
            for column in q.columns():
                column_names.append(column.name)
                column_types.append(_pg_result_type(column.type))

            # build SQL
            column_names = _rename_duplicated_column_names(column_names)
            create_sql = _build_create_temp_table_sql(result_table, column_names, column_types)
            insert_sql = _build_insert_into_sql(result_table, column_names)

            # run CREATE TABLE
            plpy.execute("drop table if exists " + plpy.quote_ident(result_table))
            plpy.execute(create_sql)

            # run INSERT
            _batch_insert(insert_sql, 10, column_types, q.results())
        finally:
            q.close()

    except (plpy.SPIError, presto_client.PrestoException) as e:
        # PL/Python converts an exception object in Python to an error message in PostgreSQL
        # using exception class name if exc.__module__ is either of "builtins", "exceptions",
        # or "__main__". Otherwise using "module.name" format. Set __module__ = "__module__"
        # to generate pretty messages.
        e.__class__.__module__ = "__main__"
        raise

def run_system_catalog_as_temp_table(server, user, catalog, schema, login_user, login_database, result_table, query):
    try:
        client = presto_client.Client(server=server, user=user, catalog=catalog, schema=schema, time_zone=_get_session_time_zone())

        # create SQL statements which put data to system catalogs
        if SchemaCacheEntry.is_cached(server, user, catalog, schema, time.time()):
            schema_names = SchemaCacheEntry.schema_names
            statements = SchemaCacheEntry.statements
            query_cache = SchemaCacheEntry.query_cache

        else:
            # get table list
            sql = "select table_schema, table_name, column_name, is_nullable, data_type" \
                  " from information_schema.columns"
            columns, rows = client.run(sql)

            schemas = {}

            if rows is None:
                rows = []

            for row in rows:
                schema_name = row[0]
                table_name = row[1]
                column_name = row[2]
                is_nullable = row[3]
                column_type = row[4]

                if len(schema_name) > PG_NAMEDATALEN - 1:
                    plpy.warning("Schema %s is skipped because its name is longer than %d characters" % \
                            (plpy.quote_ident(schema_name), PG_NAMEDATALEN - 1))
                    continue

                tables = schemas.setdefault(schema_name, {})

                if len(table_name) > PG_NAMEDATALEN - 1:
                    plpy.warning("Table %s.%s is skipped because its name is longer than %d characters" % \
                            (plpy.quote_ident(schema_name), plpy.quote_ident(table_name), PG_NAMEDATALEN - 1))
                    continue

                columns = tables.setdefault(table_name, [])

                if len(column_name) > PG_NAMEDATALEN - 1:
                    plpy.warning("Column %s.%s.%s is skipped because its name is longer than %d characters" % \
                            (plpy.quote_ident(schema_name), plpy.quote_ident(table_name), \
                             plpy.quote_ident(column_name), PG_NAMEDATALEN - 1))
                    continue

                columns.append(Column(column_name, column_type, is_nullable))

            # generate SQL statements
            statements = []
            schema_names = []

            # create a restricted user using the same name with the login user name to pgpool2
            statements.append(
                    "do $$ begin if not exists (select * from pg_catalog.pg_roles where rolname=%s) then create role %s with login; end if; end $$" % \
                    (plpy.quote_literal(login_user), plpy.quote_ident(login_user)))

            # grant access on the all table holders to the restricted user
            statements.append("grant select on all tables in schema prestogres_catalog to %s" % \
                    plpy.quote_ident(login_user))

            table_holder_id = 0

            for schema_name, tables in sorted(schemas.items(), key=lambda (k,v): k):
                if schema_name == "sys" or schema_name == "information_schema":
                    # skip system schemas
                    continue

                schema_names.append(schema_name)

                for table_name, columns in sorted(tables.items(), key=lambda (k,v): k):
                    # table schema
                    column_names = []
                    column_types = []
                    not_nulls = []
                    for column in columns:
                        column_names.append(column.name)
                        column_types.append(_pg_table_type(column.type))
                        not_nulls.append(not column.nullable)

                    # rename table holder into the schema
                    statements.append("alter table prestogres_catalog.table_holder_%d set schema %s" % \
                            (table_holder_id, plpy.quote_ident(schema_name)))
                    statements.append("alter table %s.table_holder_%d rename to %s" % \
                            (plpy.quote_ident(schema_name), table_holder_id, plpy.quote_ident(table_name)))

                    # change columns
                    alter_sql = _build_alter_table_holder_sql(schema_name, table_name, column_names, column_types, not_nulls)
                    statements.append(alter_sql)

                    table_holder_id += 1

            # cache expires after 60 seconds
            SchemaCacheEntry.set_cache(server, user, catalog, schema, schema_names, statements, time.time() + 60)
            query_cache = {}

        query_result = query_cache.get(query)

        if query_result:
            column_names = query_result.column_names
            column_types = query_result.column_types
            result = query_result.result

        else:
            # enter subtransaction to rollback tables right after running the query
            subxact = plpy.subtransaction()
            subxact.enter()
            try:
                # drop all schemas excepting prestogres_catalog, pg_catalog, information_schema, public
                # and schema holders
                sql = "select n.nspname as schema_name from pg_catalog.pg_namespace n" \
                      " where n.nspname not in ('prestogres_catalog', 'pg_catalog', 'information_schema', 'public')" \
                      " and n.nspname not like 'prestogres_catalog_schema_holder_%'" \
                      " and n.nspname !~ '^pg_toast'"
                for row in plpy.cursor(sql):
                    plpy.execute("drop schema %s cascade" % plpy.quote_ident(row["schema_name"]))

                # alter schema holders
                schema_holder_id = 0
                for schema_name in schema_names:
                    try:
                        plpy.execute("alter schema prestogres_catalog_schema_holder_%s rename to %s" % \
                                (schema_holder_id, plpy.quote_ident(schema_name)))
                        schema_holder_id += 1
                    except:
                        # ignore error?
                        pass

                # alter table holders in prestogres_catalog schema
                for statement in statements:
                    plpy.execute(statement)

                # drop prestogres_catalog schema
                plpy.execute("drop schema prestogres_catalog cascade")

                # drop schema holders
                sql = "select n.nspname as schema_name from pg_catalog.pg_namespace n" \
                      " where n.nspname like 'prestogres_catalog_schema_holder_%'"
                for row in plpy.cursor(sql):
                    plpy.execute("drop schema %s" % plpy.quote_ident(row["schema_name"]))

                # update pg_database
                plpy.execute("update pg_database set datname=%s where datname=current_database()" % \
                        plpy.quote_literal(schema_name))

                # switch to the restricted role
                plpy.execute("set role to %s" % plpy.quote_ident(login_user))

                # run the actual query and save result
                metadata = plpy.execute(query)
                column_names = metadata.colnames()
                column_type_oids = metadata.coltypes()
                result = map(lambda row: map(row.get, column_names), metadata)

                # save result schema
                oid_to_type_name = _load_oid_to_type_name_mapping(column_type_oids)
                column_types = map(oid_to_type_name.get, column_type_oids)

                # store query cache
                query_cache[query] = QueryResult(column_names, column_types, result)

            finally:
                # rollback subtransaction
                subxact.exit("rollback subtransaction", None, None)

        column_names = _rename_duplicated_column_names(column_names)
        create_sql = _build_create_temp_table_sql(result_table, column_names, column_types)
        insert_sql = _build_insert_into_sql(result_table, column_names)

        # run CREATE TABLE and INSERT
        plpy.execute("drop table if exists " + plpy.quote_ident(result_table))
        plpy.execute(create_sql)
        _batch_insert(insert_sql, 10, column_types, result)

    except (plpy.SPIError, presto_client.PrestoException) as e:
        # Set __module__ = "__module__" to generate pretty messages.
        e.__class__.__module__ = "__main__"
        raise

def create_schema_holders(count):
    for i in range(count):
        plpy.execute("drop schema if exists prestogres_catalog_schema_holder_%d cascade" % i)
        plpy.execute("create schema prestogres_catalog_schema_holder_%d" % i)

def create_table_holders(count):
    for i in range(count):
        plpy.execute("create table prestogres_catalog.table_holder_%d ()" % i)
