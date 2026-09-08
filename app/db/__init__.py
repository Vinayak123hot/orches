"""Persistence: the connection factory and every SQL statement in the app.

    database.py      Database (connection factory), SQLITE_SCHEMA, row->dict adapters
    repositories/    one module per table group
    schema.sql       the Azure SQL schema, applied by hand / by release tooling

schema.sql and the SQLITE_SCHEMA string in database.py describe the SAME tables and must
be changed together -- they live in one folder so that is obvious. SQLite has no BIT and
no IDENTITY, which is the only reason there are two of them.

NO SQL OUTSIDE THIS PACKAGE. That is the whole point of it: the services and the domain
layer talk to repository methods, so a schema change has one blast radius, and reading
"what does this app store?" means reading one folder.

It also runs on two engines. Every statement uses '?' placeholders and the upserts are
UPDATE-then-INSERT-on-miss rather than dialect-specific MERGE, so the same code works
against local SQLite and Azure SQL -- see the two dialect variants where a row limit is
needed (LIMIT vs TOP).
"""
