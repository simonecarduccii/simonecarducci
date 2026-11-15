"""Download the latest posts of the Bohème Instagram account for the feed grid of the Bohème page.

Runs every day in the "Update site" Action. It needs the repository secret INSTAGRAM_TOKEN:
- a token from Meta Business settings (system user, "never expires"), used with graph.facebook.com, or
- an "Instagram API with Instagram Login" token (Meta app "Bohème", Genera token di accesso), used with
  graph.instagram.com. It lasts 60 days: every run renews it and saves the new one back into the secret
  INSTAGRAM_TOKEN, using the secret GH_SECRETS_PAT (a fine-grained GitHub token limited to this repository,
  permission "Secrets: read and write"). Without GH_SECRETS_PAT the token is not saved and expires.
Without the secret it does nothing, and the grid keeps the photos chosen by hand in /admin.

Output:
- the feed: img/<folder>/instagram/<id>.jpg (max 1080 px) and content/instagram/feed-<folder>.json
  {"account", "aggiornato", "post": [{"foto", "link", "data"}]}, newest first;
- posts chosen by hand: any Instagram post link written in a page of content/pagine/ (e.g. as the "foto"
  of a format, a slide of the focus or a cell of the grid) is downloaded to
  img/<folder>/instagram/scelti/<code>-<n>.jpg, where n is the slide of a carousel (the link's
  ?img_index=n, 1 when missing). The page then shows that image and links to the post;
- for the /admin picker: small previews of the latest posts in img/<folder>/instagram/mini/<code>.jpg and
  their list in content/instagram/post-<folder>.json [{"codice", "link", "data", "tipo", "mini", "testo"}];
- automatic photos: a block item with "automatico": {"attivo": true, "parole": [...], "escludi": [...],
  "colore": "bianco"|"nero"|"", "soglia": n, "slide": n, [n, ...] or "tutte"} gets, every day, the latest post whose
  caption contains one of the words (hashtags and @tags included) and whose image is mostly white or black
  when asked. Result in content/instagram/auto-<folder>.json {rule key: {"link", "foto": [...]}}.
"""
import datetime
import io
import json
import os
import re
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FOLDER = os.environ.get("IG_FOLDER", "boheme")
LIMIT = 24          # posts kept for the automatic feed
SCAN = 150          # posts read to find the ones chosen by hand
PICKER = 60         # posts shown in the /admin picker
FIELDS = "id,media_type,media_url,thumbnail_url,permalink,timestamp,caption"
IG_LINK = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|tv)/([\w-]+)/?(?:\?[^\s\"]*?img_index=(\d+))?")


def get(url, **params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}" if q else url, timeout=30) as r:
        return json.load(r)


def all_media(url, token):
    """Follow the pages of a /media listing until SCAN posts."""
    out = []
    d = get(url, fields=FIELDS, limit=50, access_token=token)
    while True:
        out += d.get("data", [])
        nxt = d.get("paging", {}).get("next")
        if not nxt or len(out) >= SCAN:
            return out[:SCAN]
        d = get(nxt)


def media_facebook(token):
    """Business token: find the Instagram account linked to the Facebook pages, then its posts."""
    user_id = os.environ.get("IG_USER_ID", "").strip()
    username = ""
    if not user_id:
        pages = get("https://graph.facebook.com/v21.0/me/accounts", fields="instagram_business_account{id,username}", access_token=token)
        for p in pages.get("data", []):
            ig = p.get("instagram_business_account")
            if ig:
                user_id, username = ig["id"], ig.get("username", "")
                break
    if not user_id:
        raise RuntimeError("no Instagram account linked to this token")
    return username, all_media(f"https://graph.facebook.com/v21.0/{user_id}/media", token), "https://graph.facebook.com/v21.0"


def media_instagram(token):
    """Instagram Login token: the account is the token's owner."""
    me = get("https://graph.instagram.com/me", fields="username", access_token=token)
    items = all_media("https://graph.instagram.com/me/media", token)
    refresh(token)
    return me.get("username", ""), items, "https://graph.instagram.com"


def refresh(token):
    """Instagram Login tokens last 60 days: renew it and save the new one into the INSTAGRAM_TOKEN secret."""
    try:
        d = get("https://graph.instagram.com/refresh_access_token", grant_type="ig_refresh_token", access_token=token)
    except Exception as e:  # e.g. a token younger than 24 hours: try again tomorrow
        print("token not refreshed:", e)
        return
    new, pat, repo = d.get("access_token"), os.environ.get("GH_SECRETS_PAT", "").strip(), os.environ.get("GITHUB_REPOSITORY", "")
    if not new:
        return
    if not (pat and repo):
        print("token refreshed but not saved: add the GH_SECRETS_PAT secret, or the token expires in 60 days")
        return
    r = subprocess.run(["gh", "secret", "set", "INSTAGRAM_TOKEN", "--repo", repo], input=new, text=True,
                       capture_output=True, env={**os.environ, "GH_TOKEN": pat})
    days = int(d.get("expires_in", 0)) // 86400
    print(f"token refreshed and saved, valid for {days} days" if r.returncode == 0 else "token refreshed but not saved: GH_SECRETS_PAT cannot write secrets")


def save_jpg(url, dest, size=(1080, 1350)):
    from PIL import Image
    with urllib.request.urlopen(url, timeout=60) as r:
        im = Image.open(io.BytesIO(r.read())).convert("RGB")
    im.thumbnail(size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=82, optimize=True, progressive=True)


def code_of(item):
    m = IG_LINK.search(item.get("permalink", ""))
    return m.group(1) if m else ""


def image_url(it):
    return it.get("thumbnail_url") if it.get("media_type") == "VIDEO" else it.get("media_url")


def slide_count(it, base, token):
    """Number of images in a post: the slides of a carousel (at most 20), 1 otherwise."""
    if it.get("media_type") != "CAROUSEL_ALBUM":
        return 1
    try:
        kids = get(f"{base}/{it['id']}/children", fields="id", access_token=token).get("data", [])
    except Exception:
        return 1
    return max(1, min(20, len(kids)))


def download_slide(it, n, base, token, dest):
    """Image n (1 = first) of a post: the slide of a carousel, the cover of a video."""
    if it.get("media_type") == "CAROUSEL_ALBUM":
        kids = get(f"{base}/{it['id']}/children", fields="media_type,media_url,thumbnail_url", access_token=token).get("data", [])
        it = kids[min(n, len(kids)) - 1] if kids else it
    save_jpg(image_url(it), dest)


def rule_of(raw):
    """The automatic rule of a block item, normalised; None when it is off. Shared with build_pages.py."""
    if not isinstance(raw, dict) or not raw.get("attivo"):
        return None
    words = lambda k: sorted({str(w).strip().lower() for w in raw.get(k) or [] if str(w).strip()})
    colour = raw.get("colore") if raw.get("colore") in ("bianco", "nero") else ""
    sl = raw.get("slide")
    # "tutte" = every slide of the carousel, in order
    slides = "tutte" if sl == "tutte" else [int(x) for x in (sl if isinstance(sl, list) else [sl]) if isinstance(x, int) and 1 <= x <= 20] or [1]
    soglia = raw.get("soglia") if isinstance(raw.get("soglia"), int) and 0 < raw.get("soglia") < 255 else 0
    if not (words("parole") or colour):
        return None
    rule = {"parole": words("parole"), "escludi": words("escludi"), "colore": colour, "soglia": soglia, "slide": slides}
    # "salta": n = skip the n newest matching posts; "quanti": n = keep the n newest (after the skipped ones).
    # Written only when set, so the key (and the downloaded picks) of the older rules stay the same.
    for k, lo, hi in (("salta", 1, 10), ("quanti", 2, 12)):
        if isinstance(raw.get(k), int) and lo <= raw.get(k) <= hi:
            rule[k] = raw[k]
    return rule


def rule_key(rule):
    import hashlib
    return hashlib.sha1(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def find_rules(node, out):
    if isinstance(node, dict):
        r = rule_of(node.get("automatico"))
        if r:
            out[rule_key(r)] = r
        for v in node.values():
            find_rules(v, out)
    elif isinstance(node, list):
        for v in node:
            find_rules(v, out)
    return out


def colour_of(it, cache):
    """Mean brightness of the outer border of the post's first image (0 black - 255 white), cached by post."""
    code = code_of(it)
    if code in cache:
        return cache[code]
    from PIL import Image, ImageStat
    try:
        with urllib.request.urlopen(image_url(it), timeout=60) as r:
            im = Image.open(io.BytesIO(r.read())).convert("L")
    except Exception:
        return None
    im.thumbnail((200, 250))
    w, h = im.size
    b = max(4, int(min(w, h) * 0.08))
    strips = [im.crop((0, 0, w, b)), im.crop((0, h - b, w, h)), im.crop((0, 0, b, h)), im.crop((w - b, 0, w, h))]
    cache[code] = round(sum(ImageStat.Stat(x).mean[0] for x in strips) / 4)
    return cache[code]


def album(it, code, base, token, out_dir, keep):
    """Every slide of a post (downloaded once), as paths from the site root."""
    kids = [it]
    if it.get("media_type") == "CAROUSEL_ALBUM":
        try:
            kids = get(f"{base}/{it['id']}/children", fields="media_type,media_url,thumbnail_url",
                       access_token=token).get("data", [])[:20] or [it]
        except Exception as e:
            print("album", code, "skipped:", e)
            return []
    out = []
    for n, kid in enumerate(kids, 1):
        dest = out_dir / "scelti" / f"{code}-{n}.jpg"
        if not dest.is_file():
            try:
                save_jpg(image_url(kid), dest)
            except Exception as e:
                print("album", code, n, "skipped:", e)
                continue
        keep.add(dest.name)
        out.append(dest.relative_to(ROOT).as_posix())
    return out


def auto(items, base, token, out_dir):
    """Latest post matching each automatic rule written in content/pagine/."""
    rules = {}
    for f in [*(ROOT / "content" / "pagine").glob("*.json"), *(ROOT / "content" / "bozze").glob("*.json")]:  # drafts too
        try:
            find_rules(json.loads(f.read_text(encoding="utf-8")), rules)
        except ValueError:
            pass
    cache_f = ROOT / "content" / "instagram" / f"colori-{FOLDER}.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.is_file() else {}
    result, keep = {}, set()
    for key, r in rules.items():
        skip, posts = 0, []
        for it in items:  # newest first
            text = (it.get("caption") or "").lower()
            if r["parole"] and not any(w in text for w in r["parole"]):
                continue
            if any(w in text for w in r["escludi"]):
                continue
            if r["colore"]:
                c = colour_of(it, cache)
                if c is None:
                    continue
                if r["colore"] == "bianco" and c < (r["soglia"] or 225):
                    continue
                if r["colore"] == "nero" and c > (r["soglia"] or 40):
                    continue
            if skip < r.get("salta", 0):
                skip += 1
                continue
            code, photos = code_of(it), []
            nums = r["slide"]
            if nums == "tutte":
                nums = list(range(1, slide_count(it, base, token) + 1))
            for n in nums:
                dest = out_dir / "scelti" / f"{code}-{n}.jpg"
                if not dest.is_file():
                    try:
                        download_slide(it, n, base, token, dest)
                    except Exception as e:
                        print("automatic post", code, "skipped:", e)
                        continue
                keep.add(dest.name)
                photos.append(dest.relative_to(ROOT).as_posix())
            if photos:
                post = {"link": it["permalink"], "data": it.get("timestamp", ""), "foto": photos,
                        "testo": it.get("caption") or ""}
                if r.get("quanti"):  # browsed post by post: every slide, for the pop-up
                    post["album"] = album(it, code, base, token, out_dir, keep) or photos
                posts.append(post)
                print(f"automatic {key}: {code}")
                if len(posts) >= r.get("quanti", 1):
                    break
        if posts:  # the first post at the top level, as before; all of them in "post"
            result[key] = {**posts[0], "post": posts}
        else:
            print(f"automatic {key}: no matching post among the latest {SCAN}")
    cache_f.parent.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps(cache, sort_keys=True) + "\n", encoding="utf-8")
    (ROOT / "content" / "instagram" / f"auto-{FOLDER}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return keep


def chosen(items, base, token, out_dir, keep=frozenset()):
    """Download the posts whose link is written in a page of content/pagine/."""
    refs = set()
    for f in [*(ROOT / "content" / "pagine").glob("*.json"), *(ROOT / "content" / "bozze").glob("*.json")]:  # drafts too
        for m in IG_LINK.finditer(f.read_text(encoding="utf-8")):
            refs.add((m.group(1), max(1, int(m.group(2) or 1))))
    by_code = {code_of(it): it for it in items}
    keep = set(keep)
    for code, n in sorted(refs):
        dest = out_dir / "scelti" / f"{code}-{n}.jpg"
        keep.add(dest.name)
        if dest.is_file():
            continue
        it = by_code.get(code)
        if not it:
            print(f"chosen post {code} not among the latest {SCAN}: skipped")
            continue
        try:
            download_slide(it, n, base, token, dest)
            print("chosen post downloaded:", dest.name)
        except Exception as e:
            print("chosen post", code, "skipped:", e)
    for f in (out_dir / "scelti").glob("*.jpg"):  # no longer used in any page
        if f.name not in keep:
            f.unlink()


def picker(items, out_dir):
    """Small previews of the latest posts, for the picker in /admin."""
    rows = []
    for it in items[:PICKER]:
        code, src = code_of(it), image_url(it)
        if not (code and src):
            continue
        dest = out_dir / "mini" / f"{code}.jpg"
        if not dest.is_file():
            try:
                save_jpg(src, dest, (360, 450))
            except Exception as e:
                print("preview", code, "skipped:", e)
                continue
        rows.append({"codice": code, "link": it["permalink"], "data": it.get("timestamp", ""),
                     "tipo": it.get("media_type", ""), "mini": dest.relative_to(ROOT).as_posix(),
                     "testo": (it.get("caption") or "")[:300]})
    if not rows:
        return
    keep = {r["mini"] for r in rows}
    for f in (out_dir / "mini").glob("*.jpg"):
        if f.relative_to(ROOT).as_posix() not in keep:
            f.unlink()
    lst = ROOT / "content" / "instagram" / f"post-{FOLDER}.json"
    lst.parent.mkdir(parents=True, exist_ok=True)
    lst.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    token = os.environ.get("INSTAGRAM_TOKEN", "").strip()
    if not token:
        print("INSTAGRAM_TOKEN not set: Instagram feed not updated")
        return 0
    errors = []
    for fetch in (media_instagram, media_facebook):
        try:
            account, items, base = fetch(token)
            break
        except Exception as e:  # try the other kind of token
            errors.append(f"{fetch.__name__}: {e}")
    else:
        print("Instagram feed not updated:", "; ".join(errors))
        return 0
    out_dir = ROOT / "img" / FOLDER / "instagram"
    chosen(items, base, token, out_dir, auto(items, base, token, out_dir))
    picker(items, out_dir)
    posts = []
    for it in items[:LIMIT]:
        src = image_url(it)
        if not (src and it.get("permalink") and str(it.get("id", "")).isdigit()):
            continue
        dest = out_dir / f'{it["id"]}.jpg'
        if not dest.is_file():
            try:
                save_jpg(src, dest)
            except Exception as e:
                print("skip post", it["id"], e)
                continue
        posts.append({"foto": dest.relative_to(ROOT).as_posix(), "link": it["permalink"], "data": it.get("timestamp", "")})
    if not posts:
        print("Instagram feed: no posts returned, keeping the previous ones")
        return 0
    keep = {p["foto"] for p in posts}
    for f in out_dir.glob("*.jpg"):  # posts no longer among the latest ones
        if f.relative_to(ROOT).as_posix() not in keep:
            f.unlink()
    feed = ROOT / "content" / "instagram" / f"feed-{FOLDER}.json"
    old = json.loads(feed.read_text(encoding="utf-8")) if feed.is_file() else {}
    if old.get("post") == posts:
        print("Instagram feed unchanged")
        return 0
    feed.parent.mkdir(parents=True, exist_ok=True)
    feed.write_text(json.dumps({"account": account, "aggiornato": datetime.date.today().isoformat(), "post": posts},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Instagram feed updated: {len(posts)} posts from @{account}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
