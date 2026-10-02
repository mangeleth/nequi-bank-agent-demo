#!/usr/bin/env bash
# Create the Core Banking ledger's database and user inside the PostgreSQL pod (ADR-0019).
# Safe to run again: it creates what is missing and sets the user's password to the value in
# Key Vault. The password travels on standard input, never on a command line or to the screen.
#
#   scripts/ledger_db.sh <namespace> <key vault name>
set -euo pipefail
NAMESPACE=$1
KEYVAULT=$2

PASSWORD=$(az keyvault secret show --vault-name "$KEYVAULT" -n ledger-password --query value -o tsv)
[[ "$PASSWORD" =~ ^[A-Za-z0-9]{20,}$ ]] || { echo "ledger-password is missing or has unexpected characters" >&2; exit 1; }

kubectl exec -i -n "$NAMESPACE" postgres-0 -- psql -q -v ON_ERROR_STOP=1 -U disputes -d postgres <<SQL
SELECT 'CREATE ROLE ledger LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledger') \gexec
ALTER ROLE ledger PASSWORD '${PASSWORD}';
SELECT 'CREATE DATABASE ledger OWNER ledger' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ledger') \gexec
-- Each user may connect only to its own database.
REVOKE CONNECT ON DATABASE ledger FROM PUBLIC;
REVOKE CONNECT ON DATABASE disputes FROM PUBLIC;
GRANT CONNECT ON DATABASE ledger TO ledger;
SQL
echo "database 'ledger' and user 'ledger' are ready"
