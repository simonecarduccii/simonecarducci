"""Download the latest posts of the Bohème Instagram account for the feed grid of the Bohème page.

Runs every day in the "Update site" Action. It needs the repository secret INSTAGRAM_TOKEN:
- a token from Meta Business settings (system user, "never expires"), used with graph.facebook.com, or
- an "Instagram API with Instagram Login" token, used with graph.instagram.com.
Without the secret it does nothing, and the grid keeps the photos chosen by hand in /admin.

Output: img/<folder>/instagram/<id>.jpg (max 1080 px) and content/instagram/feed-<folder>.json
{"account", "aggiornato", "post": [{"foto", "link", "data"}]}, newest first.
"""
import datetime
import io
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FOLDER = os.environ.get("IG_FOLDER", "boheme")
LIMIT = 24
FIELDS = "id,media_type,media_url,thumbnail_url,permalink,timestamp"


def get(url, **params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}", timeout=30) as r:
        return json.load(r)


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
    d = get(f"https://graph.facebook.com/v21.0/{user_id}/media", fields=FIELDS, limit=LIMIT, access_token=token)
    return username, d.get("data", [])


def media_instagram(token):
    """Instagram Login token: the account is the token's owner."""
    me = get("https://graph.instagram.com/me", fields="username", access_token=token)
    d = get("https://graph.instagram.com/me/media", fields=FIELDS, limit=LIMIT, access_token=token)
    try:  # keeps a 60-day token alive while the Action runs every day
        get("https://graph.instagram.com/refresh_access_token", grant_type="ig_refresh_token", access_token=token)
    except Exception:
        pass
    return me.get("username", ""), d.get("data", [])


def save_jpg(url, dest):
    from PIL import Image
    with urllib.request.urlopen(url, timeout=60) as r:
        im = Image.open(io.BytesIO(r.read())).convert("RGB")
    im.thumbnail((1080, 1350))
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=82, optimize=True, progressive=True)


def main():
    token = os.environ.get("INSTAGRAM_TOKEN", "").strip()
    if not token:
        print("INSTAGRAM_TOKEN not set: Instagram feed not updated")
        return 0
    errors = []
    for fetch in (media_facebook, media_instagram):
        try:
            account, items = fetch(token)
            break
        except Exception as e:  # try the other kind of token
            errors.append(f"{fetch.__name__}: {e}")
    else:
        print("Instagram feed not updated:", "; ".join(errors))
        return 0
    out_dir = ROOT / "img" / FOLDER / "instagram"
    posts = []
    for it in items:
        src = it.get("thumbnail_url") if it.get("media_type") == "VIDEO" else it.get("media_url")
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
