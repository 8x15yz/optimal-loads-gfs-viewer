#!/usr/bin/env python3
"""
MongoDB Atlas connection diagnostic script for EC2.
Usage:
  python3 test_mongo_conn.py --uri "mongodb+srv://<user>:<pass>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority"
Or set environment variable MONGO_URI and run without --uri.
"""
import argparse
import socket
import subprocess
import sys
import time
import certifi
from urllib.parse import urlparse
from pymongo import MongoClient
import pymongo
import dns.resolver

def print_sep():
    print("="*70)

def run_openssl_test(hostname, port=27017, timeout=8):
    cmd = [
        "openssl", "s_client",
        "-connect", f"{hostname}:{port}",
        "-servername", hostname,
        "-tls1_2"
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = "Verify return code: 0 (ok)" in p.stdout
        return ok, p.stdout.splitlines()[:15]
    except FileNotFoundError:
        return None, ["openssl not installed on system"]
    except subprocess.TimeoutExpired:
        return False, ["openssl test timed out"]

def resolve_srv(uri):
    try:
        parsed = urlparse(uri)
        host = parsed.hostname
        if not host:
            return []
        print(f"🔍 Resolving SRV records for {host} ...")
        srv_records = dns.resolver.resolve(f"_mongodb._tcp.{host}", "SRV")
        results = [r.target.to_text()[:-1] for r in srv_records]
        print("   → SRV hosts:", results)
        return results
    except Exception as e:
        print(f"   ⚠️ SRV resolution failed: {e}")
        return []

def test_mongo_connection(uri):
    print_sep()
    print(f"📡 Testing MongoDB connection to:\n{uri}")
    print_sep()
    print(f"Python {sys.version.split()[0]}, PyMongo {pymongo.__version__}")
    print_sep()

    # 1. SRV resolution check
    hosts = resolve_srv(uri)
    if not hosts:
        print("⚠️  Could not resolve SRV — check DNS or URI format.")
    else:
        print("✅ DNS resolution OK.")

    # 2. OpenSSL handshake test (first SRV host)
    if hosts:
        ok, out = run_openssl_test(hosts[0])
        print_sep()
        if ok:
            print(f"✅ OpenSSL handshake OK with {hosts[0]}")
        elif ok is False:
            print(f"❌ OpenSSL handshake FAILED with {hosts[0]}")
        else:
            print(f"⚠️ OpenSSL test not available.")
        print("\n".join(out))
    else:
        print("⚠️ Skipping openssl test — no hosts found")

    # 3. PyMongo connection
    print_sep()
    print("🔗 Trying to connect via PyMongo...")
    try:
        client = MongoClient(uri, tls=True, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=10000)
        pong = client.admin.command("ping")
        print("✅ MongoDB connection success:", pong)
    except Exception as e:
        print("❌ MongoDB connection failed:")
        print(e)
    print_sep()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", required=False, help="MongoDB Atlas URI (mongodb+srv://...)")
    args = ap.parse_args()

    uri = args.uri or os.getenv("MONGO_URI")
    if not uri:
        print("❌ No URI provided. Use --uri or set MONGO_URI environment variable.")
        sys.exit(1)

    test_mongo_connection(uri)
