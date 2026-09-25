"""End-to-end public synthetic PDF canary. No private documents are transmitted."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import httpx
import pymupdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--large-page', action='store_true')
    args = parser.parse_args()
    with pymupdf.open() as doc:
        width, height = (2592, 1728) if args.large_page else (288, 216)
        p = doc.new_page(width=width, height=height)
        p.insert_text((24, 60), 'PUBLIC SYNTHETIC TEST', fontsize=15)
        p.insert_text((24, 120), 'COVERED CONTENT', fontsize=15)
        p.draw_rect((20, 95, 250, 130), color=None, fill=(1, 1, 1))
        p.draw_rect((200, 20, 220, 40), color=None, fill=(251/255,)*3)
        p.draw_rect((240, 20, 260, 40), color=None, fill=(4/255,)*3)
        if args.large_page:
            for i in range(60):
                y = 160+i*22
                p.insert_text((40, y), 'ARCHITECTURAL PAGE TEST / 36 x 24 INCHES / LINE '+str(i), fontsize=12)
                p.draw_line((600, y), (2400, y+130), color=(.2,.2,.2), width=.3)
        data = doc.tobytes()
    with httpx.Client(base_url=args.url.rstrip('/'), timeout=60) as client:
        assert client.get('/healthz').json()['status'] == 'ok'
        response = client.post('/api/jobs', json={'name':'public-canary.pdf','size':len(data)})
        response.raise_for_status()
        row = response.json()
        path = '/api/jobs/'+row['id']
        auth = {'Authorization': 'Bearer '+row['token']}
        started = time.monotonic()
        try:
            halfway = len(data)//2
            for offset, chunk in ((0,data[:halfway]), (halfway,data[halfway:])):
                r=client.patch(path+'/upload', headers={**auth, 'Upload-Offset':str(offset)}, content=chunk)
                r.raise_for_status()
            client.post(path+'/complete',headers=auth).raise_for_status()
            deadline=time.monotonic()+600
            last=None
            while time.monotonic()<deadline:
                status=client.get(path,headers=auth).json()
                if status['status']!=last:
                    print(json.dumps({k:status[k] for k in ('status','completed','total')}),flush=True)
                    last=status['status']
                if status['status'] in ('done','error'):
                    break
                time.sleep(1)
            assert status['status']=='done',status
            response=client.get(path+'/download',headers=auth)
            response.raise_for_status()
            with pymupdf.open(stream=response.content) as out:
                assert len(out)==1 and not out[0].get_text() and not out[0].get_drawings()
                images=out[0].get_images()
                assert len(images)==1 and images[0][7]=='Im0'
                import numpy as np
                pix=pymupdf.Pixmap(out,images[0][0])
                pixels=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3)
                assert np.all(pixels[::2,::2]==pixels[1::2,1::2])
                assert np.all(pixels[510:530,110:800]==255)
                # Prove the running worker includes the common tone cleanup.
                assert np.all(pixels[110:140,850:900]==255)
                assert np.all(pixels[110:140,1010:1070]==0)
            print(json.dumps({'verified':True,'output_bytes':len(response.content),'seconds':round(time.monotonic()-started,2),'sha256':hashlib.sha256(response.content).hexdigest()}),flush=True)
        finally:
            client.post(path+'/cancel',headers=auth).raise_for_status()

if __name__=='__main__':
    main()
