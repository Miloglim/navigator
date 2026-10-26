"""Autonomous pending business review queue; business data is read-only here."""
import argparse
import json
import logging
import sys
from pending_quality.core.contracts import DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE
from pending_quality.transport import runner
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO,format='[%(asctime)s] [%(levelname)s] [pending_quality] %(message)s')
def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    nxt=commands.add_parser('next');nxt.add_argument('--limit',type=int,default=DEFAULT_BATCH_SIZE)
    done=commands.add_parser('finish');done.add_argument('--claim',required=True);done.add_argument('--report',required=True)
    commands.add_parser('status')
    refresh=commands.add_parser('refresh');refresh.add_argument('--claim',required=True)
    args=parser.parse_args()
    try:
        if args.command=='next':
            if not 1<=args.limit<=MAX_BATCH_SIZE:raise ValueError('limit outside bounded batch size')
            data=runner.next_batch(args.limit)
        elif args.command=='finish':data=runner.finish(args.claim,args.report)
        elif args.command=='refresh':data=runner.refresh(args.claim)
        else:data=runner.status()
        print(json.dumps({'ok':True,'data':data},ensure_ascii=False));return 0
    except Exception as exc:
        logging.error('%s',exc)
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));return 1
if __name__=='__main__':raise SystemExit(main())
