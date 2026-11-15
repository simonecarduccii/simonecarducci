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
  their list in content/instagram/post-<folder>.json [{"codice", "link", "data", "tipo", "mini"}].
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
FIELDS = "id,media_type,media_url,thumbnail_url,permalink,timestamp"
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


def chosen(items, base, token, out_dir):
    """Download the posts whose link is written in a page of content/pagine/."""
    refs = set()
    for f in (ROOT / "content" / "pagine").glob("*.json"):
        for m in IG_LINK.finditer(f.read_text(encoding="utf-8")):
            refs.add((m.group(1), max(1, int(m.group(2) or 1))))
    by_code = {code_of(it): it for it in items}
    keep = set()
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
            if it.get("media_type") == "CAROUSEL_ALBUM":
                kids = get(f"{base}/{it['id']}/children", fields="media_type,media_url,thumbnail_url", access_token=token).get("data", [])
                it = kids[min(n, len(kids)) - 1] if kids else it
            save_jpg(image_url(it), dest)
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
                     "tipo": it.get("media_type", ""), "mini": dest.relative_to(ROOT).as_posix()})
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
    chosen(items, base, token, out_dir)
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
