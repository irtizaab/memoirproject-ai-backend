import os
import sys

from dotenv import load_dotenv
import psycopg

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

if len(sys.argv) < 2:
    print("usage: python migrate.py migrations/0001_slice1.sql")
    sys.exit(1)

path = sys.argv[1]

with open(path) as f:
    sql = f.read()

print(f"applying {path} ...")

with psycopg.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute(sql)

print("done")