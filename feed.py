import os, re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.dom import minidom

USER = "hackrabbits"
FILTER = "complete"
BASE = "https://nyaa.si"
FEED_FILE = "feed.xml"
UA = "Mozilla/5.0 (compatible; hackrabbits-rss-bot/1.0)"

def parse_date(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return None

def scrape_page(page=1):
    url = f"{BASE}/user/{USER}?page={page}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table > tbody > tr")
    if not rows:
        return []
    results = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        title_cell = cells[1]
        title_link = title_cell.find("a", href=re.compile(r"^/view/\d+"))
        if not title_link:
            continue
        tid_match = re.search(r"/view/(\d+)", title_link["href"])
        if not tid_match:
            continue
        tid = tid_match.group(1)
        title = (title_link.get("title") or title_link.text).strip()
        magnet_tag = cells[2].find("a", href=lambda h: h and h.startswith("magnet:"))
        magnet = magnet_tag["href"] if magnet_tag else ""
        torrent_tag = cells[2].find("a", href=lambda h: h and h.endswith(".torrent"))
        torrent_url = BASE + torrent_tag["href"] if torrent_tag else ""
        time_tag = cells[3].find("time") if len(cells) > 3 else None
        pub_date = ""
        if time_tag and time_tag.get("datetime"):
            pub_date = parse_date(time_tag["datetime"])
        if not pub_date:
            pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        results.append({
            "tid": tid, "title": title,
            "link": f"{BASE}/view/{tid}",
            "magnet": magnet, "torrent_url": torrent_url,
            "pub_date": pub_date,
        })
    return results

def load_existing():
    if not os.path.exists(FEED_FILE):
        return []
    try:
        tree = ET.parse(FEED_FILE)
        root = tree.getroot()
        entries = []
        for item in root.findall(".//item"):
            guid = item.find("guid")
            if guid is None or not guid.text:
                continue
            tid = guid.text.replace("nyaa-", "")
            desc = item.find("description")
            magnet = ""
            if desc is not None and desc.text:
                m = re.search(r"(magnet:\?xt=urn:btih:[^\s<]+)", desc.text)
                if m:
                    magnet = m.group(1)
            entries.append({
                "tid": tid,
                "title": item.findtext("title", ""),
                "link": item.findtext("link", ""),
                "magnet": magnet,
                "torrent_url": "",
                "pub_date": item.findtext("pubDate", ""),
            })
        return entries
    except Exception:
        return []

def build_rss(entries):
    rss = ET.Element("rss", version="2.0",
                     attrib={"xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{USER} - {FILTER.title()} Releases"
    ET.SubElement(channel, "link").text = f"{BASE}/user/{USER}"
    ET.SubElement(channel, "description").text = \
        f"Persistent filtered RSS feed for {USER}'s releases containing '{FILTER}' on Nyaa"
    a = ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    a.set("href",
          "https://mid-anxious.github.io/hackrabbits-rss/feed.xml")
    a.set("rel", "self")
    a.set("type", "application/rss+xml")
    for e in reversed(entries):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = e["title"]
        ET.SubElement(item, "link").text = e["link"]
        g = ET.SubElement(item, "guid")
        g.set("isPermaLink", "false")
        g.text = f"nyaa-{e['tid']}"
        ET.SubElement(item, "pubDate").text = e["pub_date"]
        desc = ET.SubElement(item, "description")
        desc.text = e.get("magnet", "")
    raw = ET.tostring(rss, encoding="unicode")
    dom = minidom.parseString(raw.encode())
    return dom.toprettyxml(indent="  ")

def main():
    existing = load_existing()
    seen = {e["tid"] for e in existing}
    print(f"Loaded {len(existing)} existing entries")
    all_new = []
    page = 1
    while True:
        print(f"Scraping page {page} ...")
        entries = scrape_page(page)
        if not entries:
            print("  No more pages")
            break
        unknown = [e for e in entries if e["tid"] not in seen]
        if not unknown:
            print("  No new IDs – caught up")
            break
        matched = [e for e in unknown if FILTER.lower() in e["title"].lower()]
        print(f"  Got {len(unknown)} new, {len(matched)} match filter")
        all_new.extend(matched)
        seen.update(e["tid"] for e in unknown)
        page += 1
    if not all_new and existing:
        print("Nothing new")
        return
    merged = all_new + existing
    merged.sort(key=lambda e: e["pub_date"])
    print(f"Writing {len(merged)} entries to {FEED_FILE}")
    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(build_rss(merged))

if __name__ == "__main__":
    main()
