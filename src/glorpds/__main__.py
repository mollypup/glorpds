"""glorpds CLI

Usage:
  glorpds init <hostname> [--dev | --sandbox]
  glorpds config [--pds_pfx=URL] [--pds_did=DID] [--bsky_appview_pfx=URL] [--bsky_appview_did=DID]
  glorpds account create <did> <handle> [--unsafe_password=PW] [--signing_key=PEM]
  glorpds run [--sock_path=PATH] [--listen_host=HOST] [--listen_port=PORT]
  glorpds util keygen [--p256 | --k256]
  glorpds util print_pubkey <pem>
  glorpds util plcgen --genesis_json=PATH --rotation_key=PEM --handle=HANDLE --pds_host=URL --repo_pubkey=DIDKEY
  glorpds util plcsign --unsigned_op=PATH --rotation_key=PEM [--prev_op=PATH]
  glorpds (-h | --help)
  glorpds --version

Init:
  Initialise the database. Must be done before any other commands will work.
  This also sets the config options to their defaults.

  hostname            The public-facing hostname of this PDS, e.g. "bsky.social"
  --dev               Pre-set config options for local dev/testing
  --sandbox           Pre-set config options to work with the bsky "sandbox" network. Otherwise, default to bsky prod.

Config:
  Any options not specified will be left at their previous values. Once changes
  have been made (or even if they haven't), the new config will be printed.

  Do not change the config while the PDS is running (TODO: enforce this in code (or make sure it's harmless?))

  --pds_pfx=URL           The HTTP URL prefix that this PDS is publicly accessible at (e.g. mypds.example)
  --pds_did=DID           This PDS's DID (e.g. did:web:mypds.example)
  --bsky_appview_pfx=URL  AppView URL prefix e.g. "https://api.bsky-sandbox.dev"
  --bsky_appview_did=DID  AppView DID e.g. did:web:api.bsky-sandbox.dev

Account Create:
  Create a new user account on the PDS. Bring your own DID and corresponding
  handle - glorpds will not (yet?) attempt to validate either.
  You'll be prompted for a password interactively.

  --unsafe_password=PW  Specify password non-iteractively, for use in test scripts etc.
  --signing_key=PEM     Path to a PEM file

Run:
  Launch the service (in the foreground)

  --sock_path=PATH    UNIX domain socket to listen on (supersedes host and port options if specified)
  --listen_host=HOST  Hostname to listen on [default: 127.0.0.1]
  --listen_port=PORT  TCP port to listen on [default: 8123]

Util Keygen:
  Generate a signing key, save it to a PEM, and print its path to stdout.

  --p256    NISTP256 key format (default)
  --k256    secp256k1 key format

General options:
  -h --help           Show this screen.
  --version           Show version.
"""

import importlib.metadata
import asyncio
import logging
import json
import base64
import hashlib
import urllib.parse
from getpass import getpass

from docopt import docopt
from .ssrf import get_ssrf_safe_client


import cbrrr

from . import service
from . import database
from . import crypto
from . import util


logging.basicConfig(level=logging.DEBUG)  # TODO: make this configurable?


def main():
    """
    This is the entrypoint for the `glorpds` command (declared in project.scripts)
    """

    args = docopt(
        __doc__,
        version=f"glorpds version {importlib.metadata.version('glorpds')}",
    )

    if args["init"]:
        init(hostname=args["<hostname>"],
             dev=args["--dev"], sandbox=args["--sandbox"])

    elif args["util"]:
        if args["keygen"]:  # TODO: deprecate in favour of openssl?
            keygen(args["--k256"])
        elif args["print_pubkey"]:
            print_pubkey(args["<pem>"])
        elif args["plcgen"]:
            plcgen(
                rotation_key=args["--rotation_key"],
                repo_pubkey=args["--repo_pubkey"],
                handle=args["--handle"],
                pds_host=args["--pds_host"],
                genesis_json=args["--genesis_json"]
            )
        elif args["plcsign"]:
            plcsign(
                unsigned_op=args["--unsigned_op"],
                rotation_key=args["--rotation_key"],
                prev_op=args["--prev_op"]
            )
        else:
            print("invalid util subcommand")
        return

    # everything after this point requires an already-inited db
    db = database.Database()
    if not db.config_is_initialised():
        print("Config uninitialised! Try the `init` command")
        return

    if args["config"]:
        config(
            db=db,
            pds_pfx=args["--pds_pfx"],
            pds_did=args["--pds_did"],
            bsky_appview_pfx=args["--bsky_appview_pfx"],
            bsky_appview_did=args["--bsky_appview_did"]
        )
    elif args["account"]:
        if args["create"]:
            account_create(
                db=db,
                did=args["<did>"],
                handle=args["<handle>"],
                unsafe_password=args["--unsafe_password"],
                signing_key=args["--signing_key"]
            )
        else:
            print("invalid account subcommand")
    elif args["run"]:
        run(
            db=db,
            sock_path=args["--sock_path"],
            host=args["--listen_host"],
            port=args["--listen_port"]
        )
    else:
        print("CLI arg parse error?!")


def init(hostname, dev=False, sandbox=False):
    db = database.Database()
    if db.config_is_initialised():
        print(
            "Already initialised! Use the `config` command to make changes,"
            " or manually delete the db and try again."
        )
        return
    if sandbox:  # like prod but http://
        db.update_config(
            pds_pfx=f'http://{hostname}',
            pds_did=f'did:web:{urllib.parse.quote(hostname)}',
            bsky_appview_pfx="https://api.bsky.app",
            bsky_appview_did="did:web:api.bsky.app",
        )
    elif dev:  # now-defunct, need to figure out how to point at local infra
        db.update_config(
            pds_pfx=f'https://{hostname}',
            pds_did=f'did:web:{urllib.parse.quote(hostname)}',
            bsky_appview_pfx="https://api.bsky-sandbox.dev",
            bsky_appview_did="did:web:api.bsky-sandbox.dev",
        )
    else:  # "prod" presets
        db.update_config(
            pds_pfx=f'https://{hostname}',
            pds_did=f'did:web:{urllib.parse.quote(hostname)}',
            bsky_appview_pfx="https://api.bsky.app",
            bsky_appview_did="did:web:api.bsky.app",
        )
    assert db.config_is_initialised()
    db.print_config()
    return


def keygen(k256=False):
    if k256:
        privkey = (
            crypto.keygen_k256()
        )  # openssl ecparam -name secp256k1 -genkey -noout
    else:  # default
        privkey = (
            crypto.keygen_p256()
        )  # openssl ecparam -name prime256v1 -genkey -noout
    print(crypto.privkey_to_pem(privkey), end="")


def print_pubkey(pem_arg):
    with open(pem_arg) as pem:
        pem_data = pem.read()
    try:
        pubkey = crypto.privkey_from_pem(pem_data).public_key()
    except ValueError:
        pubkey = crypto.pubkey_from_pem(pem_data)
    print(crypto.encode_pubkey_as_did_key(pubkey))


def plcgen(rotation_key, repo_pubkey, handle, pds_host, genesis_json):
    with open(rotation_key) as pem:
        rotation_key = crypto.privkey_from_pem(pem.read())
    if not repo_pubkey.startswith("did:key:z"):
        raise ValueError("invalid did:key")
    genesis = {
        "type": "plc_operation",
        "rotationKeys": [
                crypto.encode_pubkey_as_did_key(
                    rotation_key.public_key())
        ],
        "verificationMethods": {"atproto": repo_pubkey},
        "alsoKnownAs": ["at://" + handle],
        "services": {
            "atproto_pds": {
                "type": "AtprotoPersonalDataServer",
                "endpoint": pds_host,
            }
        },
        "prev": None,
    }
    genesis["sig"] = crypto.plc_sign(rotation_key, genesis)
    genesis_digest = hashlib.sha256(
        cbrrr.encode_dag_cbor(genesis)
    ).digest()
    plc = (
        "did:plc:"
        + base64.b32encode(genesis_digest)[:24].lower().decode()
    )
    with open(genesis_json, "w") as out:
        json.dump(genesis, out, indent=4)
    print(plc)


def plcsign(unsigned_op, rotation_key, prev_op):
    with open(unsigned_op) as op_json:
        op = json.load(op_json)
    with open(rotation_key) as pem:
        rotation_key = crypto.privkey_from_pem(pem.read())
    if prev_op:
        with open(prev_op) as op_json:
            prev_op = json.load(op_json)
        op["prev"] = cbrrr.CID.cidv1_dag_cbor_sha256_32_from(
            cbrrr.encode_dag_cbor(prev_op)
        ).encode()
    del op["sig"]  # remove any existing sig
    op["sig"] = crypto.plc_sign(rotation_key, op)
    print(json.dumps(op, indent=4))


def config(db, pds_pfx, pds_did, bsky_appview_pfx, bsky_appview_did):
    db.update_config(
        pds_pfx=pds_pfx,
        pds_did=pds_did,
        bsky_appview_pfx=bsky_appview_pfx,
        bsky_appview_did=bsky_appview_did,
    )
    db.print_config()


def account_create(db, did, handle, signing_key, unsafe_password=None):
    pw = unsafe_password
    if pw is not None:
        print(
            "WARNING: passing a password as a CLI arg is not recommended, for security"
        )
    else:
        pw = getpass("Password for new account: ")
        if getpass("Confirm password: ") != pw:
            print("error: password mismatch")
            return
    pem_path = signing_key
    if pem_path:
        privkey = crypto.privkey_from_pem(open(pem_path).read())
    else:
        privkey = crypto.keygen_p256()
    db.create_account(
        did=did,
        handle=handle,
        password=pw,
        privkey=privkey,
    )


def run(db, sock_path, host, port):
    async def run_service_with_client():
        # TODO: option to use regular unsafe client for local dev testing
        async with get_ssrf_safe_client() as client:
            await service.run(
                db=db,
                client=client,
                sock_path=sock_path,
                host=host,
                port=int(port),
            )

    asyncio.run(run_service_with_client())


"""
This is the entrypoint for python3 -m glorpds
"""
if __name__ == "__main__":
    main()
