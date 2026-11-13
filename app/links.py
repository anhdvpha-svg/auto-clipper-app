"""Saved source links, grouped by project / campaign (stored in the app's data folder)."""
import json, os, time

from . import config

KEEP = ("url", "title", "platform", "channel", "views", "duration", "thumb", "hot", "ts", "caption")


def _path():
    return os.path.join(config.data_dir(), "saved_links.json")


def load():
    try:
        d = json.load(open(_path(), encoding="utf-8"))
        if isinstance(d.get("projects"), list):
            return d
    except Exception:
        pass
    return {"projects": [], "last": None}


def save(d):
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def project(d, name, create=True):
    p = next((p for p in d["projects"] if p["name"] == name), None)
    if not p and create:
        p = {"name": name, "created": time.time(), "items": []}
        d["projects"].insert(0, p)
    return p


def add(name, item):
    """Save a link into a project; returns False when it was already there."""
    d = load()
    p = project(d, name)
    d["last"] = name
    if any(x["url"] == item["url"] for x in p["items"]):
        save(d)
        return False
    x = {k: item.get(k) for k in KEEP if item.get(k) is not None}
    x["saved"] = time.time()
    p["items"].insert(0, x)
    save(d)
    return True


def remove(name, url):
    d = load()
    p = project(d, name, create=False)
    if p:
        p["items"] = [x for x in p["items"] if x["url"] != url]
        save(d)


def update(name, url, **fields):
    d = load()
    p = project(d, name, create=False)
    for x in (p or {}).get("items", []):
        if x["url"] == url:
            x.update({k: v for k, v in fields.items() if v is not None})
    save(d)


def rename(old, new):
    d = load()
    p = project(d, old, create=False)
    if p and new and not project(d, new, create=False):
        p["name"] = new
        if d.get("last") == old:
            d["last"] = new
        save(d)
        return True
    return False


def delete(name):
    d = load()
    d["projects"] = [p for p in d["projects"] if p["name"] != name]
    if d.get("last") == name:
        d["last"] = None
    save(d)


def names():
    return [p["name"] for p in load()["projects"]]
