import os, re, sys, argparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.dom import minidom

USER = "hackrabbits"
BASE = "https://nyaa.si"
UA = "Mozilla/5.0 (compatible; hackrabbits-rss-bot/1.0)"


def parse_date(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        pass
    try:
        dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
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
        if len(cells) < 5:
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
        pub_date = ""
        if len(cells) > 4:
            pub_date = parse_date(cells[4].get_text(strip=True))
        if not pub_date:
            pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        results.append({
            "tid": tid, "title": title,
            "link": f"{BASE}/view/{tid}",
            "magnet": magnet, "torrent_url": torrent_url,
            "pub_date": pub_date,
        })
    return results


def load_existing(feed_file):
    if not os.path.exists(feed_file):
        return [], set()
    try:
        tree = ET.parse(feed_file)
        root = tree.getroot()
        entries = []
        for item in root.findall(".//item"):
            guid = item.find("guid")
            if guid is None or not guid.text:
                continue
            tid = re.search(r"/view/(\d+)", guid.text)
            if not tid:
                continue
            tid = tid.group(1)
            link = item.findtext("link", "")
            desc = item.find("description")
            magnet = ""
            if desc is not None and desc.text:
                m = re.search(r"(magnet:\?xt=urn:btih:[^\s<]+)", desc.text)
                if m:
                    magnet = m.group(1)
            torrent_url = link if link.endswith(".torrent") else f"{BASE}/download/{tid}.torrent"
            entries.append({
                "tid": tid, "title": item.findtext("title", ""),
                "link": link, "magnet": magnet,
                "torrent_url": torrent_url,
                "pub_date": item.findtext("pubDate", ""),
            })
        seen = {e["tid"] for e in entries}
        return entries, seen
    except Exception:
        return [], set()


def info_hash_from_magnet(magnet):
    m = re.search(r"btih:([a-fA-F0-9]{40})", magnet)
    return m.group(1).lower() if m else ""


def build_rss(entries, filter_text, feed_file, pages_url):
    NS_NYAA = "https://nyaa.si/xmlns/nyaa"
    label = f" ({filter_text})" if filter_text else ""
    rss = ET.Element("rss", version="2.0",
                     attrib={"xmlns:atom": "http://www.w3.org/2005/Atom",
                             "xmlns:nyaa": NS_NYAA})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{USER}{label}"
    ET.SubElement(channel, "link").text = f"{BASE}/user/{USER}"
    ET.SubElement(channel, "description").text = \
        f"Persistent RSS feed for {USER}'s releases{' containing ' + filter_text if filter_text else ''} on Nyaa"
    a = ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    a.set("href", pages_url + feed_file)
    a.set("rel", "self")
    a.set("type", "application/rss+xml")
    for e in reversed(entries):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = e["title"]
        ET.SubElement(item, "link").text = e.get("torrent_url",
                                                  f"{BASE}/download/{e['tid']}.torrent")
        g = ET.SubElement(item, "guid")
        g.set("isPermaLink", "true")
        g.text = f"{BASE}/view/{e['tid']}"
        ET.SubElement(item, "pubDate").text = e["pub_date"]
        ih = info_hash_from_magnet(e.get("magnet", ""))
        if ih:
            ET.SubElement(item, f"{{{NS_NYAA}}}infoHash").text = ih
        desc = ET.SubElement(item, "description")
        desc.text = e.get("magnet", "")
        torr = e.get("torrent_url", f"{BASE}/download/{e['tid']}.torrent")
        enc = ET.SubElement(item, "enclosure")
        enc.set("url", torr)
        enc.set("type", "application/x-bittorrent")
        enc.set("length", "0")
    raw = ET.tostring(rss, encoding="unicode")
    dom = minidom.parseString(raw.encode())
    return dom.toprettyxml(indent="  ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="complete",
                        help="Filter string (empty = no filter)")
    parser.add_argument("--output", default="feed.xml",
                        help="Output feed file")
    parser.add_argument("--pages-url", default="https://mid-anxious.github.io/hackrabbits-rss/",
                        help="GitHub Pages base URL")
    args = parser.parse_args()

    filter_text = args.filter
    feed_file = args.output

    existing, seen = load_existing(feed_file)
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
        if filter_text:
            matched = [e for e in unknown if filter_text.lower() in e["title"].lower()]
        else:
            matched = unknown
        print(f"  Got {len(unknown)} new, {len(matched)} match filter")
        all_new.extend(matched)
        seen.update(e["tid"] for e in unknown)
        page += 1

    if not all_new and existing:
        print("Nothing new")
        return

    merged = all_new + existing
    merged.sort(key=lambda e: e["pub_date"])
    print(f"Writing {len(merged)} entries to {feed_file}")
    with open(feed_file, "w", encoding="utf-8") as f:
        f.write(build_rss(merged, filter_text, feed_file, args.pages_url))


if __name__ == "__main__":
    main()
