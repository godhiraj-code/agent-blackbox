"""Run the exact scenario over real loopback HTTP and validate its trace."""
import json, threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import server

def call(base, path, method="GET", body=None):
    raw=json.dumps(body).encode() if body is not None else None
    req=Request(base+path,data=raw,method=method,headers={"Content-Type":"application/json"})
    try:
        with urlopen(req,timeout=3) as r:return r.status,json.load(r)
    except HTTPError as e:return e.code,json.load(e)

def main():
    httpd=server.make_server(port=0);threading.Thread(target=httpd.serve_forever,daemon=True).start();base=f"http://127.0.0.1:{httpd.server_port}"
    try:
        call(base,"/api/reset","POST",{})
        status,_=call(base,"/api/config","POST",{"value":"on","version":6});assert status==409
        _,state=call(base,"/api/state");assert state["value"]=="off" and state["version"]==7
        server.event("ui_toast","Green Saved toast rendered",color="green")
        server.event("agent_claim","Agent claimed success",claim="completed")
        server.event("contradiction","Claim says on; backend remains off",claimed="on",actual="off")
        status,_=call(base,"/api/config","POST",{"value":"on","version":6});assert status==409
        _,fresh=call(base,"/api/state")
        status,_=call(base,"/api/config","POST",{"value":"on","version":fresh["version"]});assert status==200
        _,final=call(base,"/api/state");assert final["value"]=="on" and final["version"]==8
        server.event("verification","Independent backend read matches requested value",verified=True,value="on")
        rows=[json.loads(x) for x in server.TRACE.read_text(encoding="utf-8").splitlines()]
        kinds={x["event"] for x in rows};assert {"http_response","contradiction","verification"}<=kinds
        print(f"PASS: 409 preserved state; safe retry persisted; {len(rows)} evidence events; verification=true")
    finally:httpd.shutdown();httpd.server_close()
if __name__=="__main__":main()
