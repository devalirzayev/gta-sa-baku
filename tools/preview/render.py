"""Render a preview of DFF models with three.js in headless Chromium (Playwright).

scene: list of (dff_bytes, position xyz, quaternion xyzw or None) + camera.
Textures are taken from a directory of PNGs (same names as in the DFF materials).
"""
import json, os, shutil, subprocess, sys, time, socket, threading, http.server, functools
import numpy as np
from tools.rw import dff as rwdff

HERE = os.path.dirname(os.path.abspath(__file__))


def scene_json(models, eye, target, fov=60, fog=None, sky="#9db8d0"):
    meshes = []
    for item in models:
        data, pos = item[0], item[1]
        q = item[2] if len(item) > 2 else None
        r = rwdff.read_dff(data)
        g = r["geometries"][0]
        V = g["vertices"].astype(float)
        UV = g["uvs"][0].astype(float)
        C = g["prelit"][:, :3].astype(float) / 255.0 if "prelit" in g else None
        for mi, mat in enumerate(g["materials"]):
            sel = g["mat_ids"] == mi
            if not sel.any():
                continue
            T = g["triangles"][sel]
            used, inv = np.unique(T.ravel(), return_inverse=True)
            m = {"p": V[used].ravel().round(3).tolist(), "uv": UV[used].ravel().round(4).tolist(),
                 "i": inv.tolist(), "t": mat["texture"], "pos": list(map(float, pos))}
            if C is not None:
                m["c"] = (np.clip(C[used] * 1.25, 0, 1)).ravel().round(3).tolist()
            if q is not None:
                m["q"] = list(map(float, q))
            if mat["texture"] in ("bk_zebra",):
                m["alpha"] = True
            meshes.append(m)
    return {"meshes": meshes, "eye": list(map(float, eye)), "target": list(map(float, target)), "fov": fov,
            "fog": fog, "sky": sky}


def render(scene, tex_dir, out_png, w=1600, h=900, workdir=None):
    workdir = workdir or os.path.join(os.path.dirname(out_png), "_viewer")
    os.makedirs(workdir, exist_ok=True)
    three = os.environ.get("THREE_JS", "/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/scratchpad/viewer/node_modules/three/build/three.module.js")
    shutil.copy(three, os.path.join(workdir, "three.module.js"))
    shutil.copy(os.path.join(HERE, "viewer.html"), os.path.join(workdir, "viewer.html"))
    if os.path.islink(os.path.join(workdir, "tex")) or os.path.exists(os.path.join(workdir, "tex")):
        if os.path.islink(os.path.join(workdir, "tex")):
            os.unlink(os.path.join(workdir, "tex"))
        else:
            shutil.rmtree(os.path.join(workdir, "tex"))
    os.symlink(os.path.abspath(tex_dir), os.path.join(workdir, "tex"))
    with open(os.path.join(workdir, "scene.json"), "w") as f:
        json.dump(scene, f)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=workdir)
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    js = f"""
const {{ chromium }} = require('playwright');
(async () => {{
  const b = await chromium.launch({{args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']}});
  const p = await b.newPage({{viewport:{{width:{w},height:{h}}}}});
  p.on('console', m => console.log('console:', m.text()));
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto('http://127.0.0.1:{port}/viewer.html?w={w}&h={h}');
  await p.waitForFunction('window.done === true', null, {{timeout: 300000}});
  await p.screenshot({{path: '{out_png}'}});
  await b.close();
}})();
"""
    jsf = os.path.join(workdir, "shot.js")
    with open(jsf, "w") as f:
        f.write(js)
    env = dict(os.environ, NODE_PATH="/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/scratchpad/viewer/node_modules:" + os.environ.get("NODE_PATH", ""))
    res = subprocess.run(["node", jsf], capture_output=True, text=True, env=env, timeout=600)
    srv.shutdown()
    if res.returncode != 0:
        raise RuntimeError(res.stdout + res.stderr)
    return res.stdout
