"""Explicitly gated one-shot real relay; never starts a listener on import.

Run: python -m siri_cli.relay_live --config approved.json --live
Config must also contain live_test_approved:true. No shipped config enables IO.
First run bootstraps a history cutoff. Subsequent invocations poll scoped rows.
"""
import argparse
import fcntl
import json
from pathlib import Path
import sqlite3
from .messages_adapter import Route,MessagesInbox,MessagesSender
from .messages_live import AppleMessagesTransport
from .preview_provider import SiriPreviewProvider,NativePreviewPort
from .relay import Relay,RelayFailure


def run(config,*,live=False):
    if not live or config.get('live_test_approved') is not True:raise RelayFailure('live_mode_not_approved')
    r=dict(config['route'])
    for key in ('participants','self_handles'):r[key]=frozenset(r[key])
    route=Route(**r);route.validate()
    root=Path(config['state_dir']).expanduser();root.mkdir(parents=True,exist_ok=True)
    with open(root/'runner.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RelayFailure('relay_runner_busy')
        source=sqlite3.connect(Path(config['database']).expanduser().resolve().as_uri()+'?mode=ro',uri=True)
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        try:inbox=MessagesInbox(source,root/'inbox.sqlite',route,live_enabled=True)
        finally:source.rollback()
        wire=AppleMessagesTransport(source,root/'wire',route,live_enabled=True)
        sender=MessagesSender(root/'sender.sqlite',route,wire,live_enabled=True)
        relay=Relay(root/'relay.sqlite',root/'artifacts',SiriPreviewProvider(NativePreviewPort(live_enabled=True)),sender)
        # Recover relay and sender crash boundaries independently. A sender may
        # already have its receipt while the relay still says sending.
        from .relay import Incoming
        unresolved=False
        for envelope, in inbox.state.execute('SELECT envelope FROM inbox').fetchall():
            incoming=Incoming(**json.loads(envelope))
            if not incoming.query:continue
            key=json.dumps([incoming.transport,incoming.message_id])
            row=relay.db.execute('SELECT status FROM requests WHERE key=?',(key,)).fetchone()
            if row and row[0] in ('sending','delivery_uncertain'):
                status=relay.reconcile_delivery(incoming)['status']
                unresolved |= status!='sent_reconciled'
        if unresolved:raise RelayFailure('unresolved_send_blocks_live_queue')
        source.execute('BEGIN')
        try:inbox.poll_once()
        finally:source.rollback()
        return inbox.dispatch_pending(relay)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--live',action='store_true')
    a=p.parse_args()
    try:result=run(json.loads(Path(a.config).read_text()),live=a.live)
    except Exception as e:raise SystemExit(str(e))
    # Status only; never dumps body, raw Siri logs or failed candidates.
    print(json.dumps([{'status':x['status']} for x in result]))

if __name__=='__main__':main()
