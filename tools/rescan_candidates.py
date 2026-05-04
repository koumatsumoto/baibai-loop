import yaml
import glob
from pathlib import Path

def rescan():
    files = glob.glob('records/03-candidates/**/*.yaml', recursive=True)
    results = []
    for f in sorted(files):
        with open(f, 'r') as stream:
            try:
                data = yaml.safe_load(stream)
                if not data or 'tickers' not in data: continue
                for t in data['tickers']:
                    pc60 = t.get('price_change_60d')
                    pf = t.get('per_forward')
                    pt = t.get('per_trailing')
                    
                    hit_pc = pc60 is not None and abs(pc60) > 0.5
                    hit_per = False
                    if pf is not None and pt is not None and pt != 0:
                        if abs(pf / pt - 1.0) > 1.0:
                            hit_per = True
                    
                    if hit_pc or hit_per:
                        results.append({
                            'file': f,
                            'ticker': t['ticker'],
                            'name': t['name'],
                            'pc60': pc60,
                            'pf': pf,
                            'pt': pt,
                            'reason': 'price' if hit_pc else 'per'
                        })
            except Exception:
                continue
    
    for r in results:
        print(f"{r['file']}: {r['ticker']} ({r['name']}) pc60={r['pc60']} pf={r['pf']} pt={r['pt']} reason={r['reason']}")

if __name__ == '__main__':
    rescan()
