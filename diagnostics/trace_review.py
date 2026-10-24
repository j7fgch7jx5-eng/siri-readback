"""Summarize diagnostic signal changes without reproducing answer contents."""
import collections
import json
from pathlib import Path


def review(path):
    rows=[json.loads(line) for line in Path(path).read_text().splitlines()]
    snapshots=[r for r in rows if r['kind'] in ('fresh','snapshot')]
    transitions=[]
    previous={}
    for row in snapshots:
        current={tuple(n['path']):n for n in row['nodes']}
        changes=[]
        for key,node in current.items():
            old=previous.get(key)
            if old is None:
                changes.append({'path':key,'role':node['role'],'identifier':node['identifier'],'change':'appeared'})
            else:
                fields=[k for k in node if node[k]!=old.get(k)]
                if fields:changes.append({'path':key,'role':node['role'],'identifier':node['identifier'],'changed_fields':fields,
                                          'state':node.get('state') if 'state' in fields else None})
        for key in previous.keys() - current.keys():
            old=previous[key]
            changes.append({'path':key,'role':old['role'],'identifier':old['identifier'],'change':'disappeared'})
        if changes:transitions.append({'elapsed_s':row['elapsed_s'],'changes':changes})
        previous=current
    registrations=collections.Counter((r['target'],r['notification'],r['error']) for r in rows if r['kind']=='registration')
    return {'run_id':rows[0]['run_id'] if rows else None,
            'lifecycle':[r for r in rows if r['kind'] in ('submitted','end','cleanup','capture')],
            'notification_counts':dict(collections.Counter(r['name'] for r in rows if r['kind']=='notification')),
            'registration_results':[{'target':t,'notification':n,'error':e,'count':c} for (t,n,e),c in registrations.items()],
            'transitions':transitions}

if __name__=='__main__':
    import sys
    print(json.dumps(review(sys.argv[1]),indent=2))
