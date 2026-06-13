import os, re, sys, argparse, calendar
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.dom import minidom

USER = "hackrabbits"
BASE = "https://nyaa.si"
UA = "Mozilla/5.0 (compatible; hackrabbits-rss-bot/1.0)"

MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}


def parse_dt(dt_str):
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            pass
    return None


def fmt_rfc2822(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def date_sort_key(pub_date_str):
    try:
        dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S +0000")
        return dt.timestamp()
    except ValueError:
        return 0


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
        # Specifically avoid links that jump to comments (#comments)
        title_link = title_cell.find("a", href=re.compile(r"^/view/\d+$"))
        if not title_link:
            # Fallback if the link is different, but ensure it's not the comment link
            title_link = title_cell.find("a", href=re.compile(r"^/view/\d+"))
            if title_link and "#comments" in title_link.get("href", ""):
                title_link = None

        if not title_link:
            continue
            
        tid_match = re.search(r"/view/(\d+)", title_link["href"])
        if not tid_match:
            continue
        tid = tid_match.group(1)
        title = (title_link.get("title") or title_link.text).strip()
        
        # If title is still trash like "1 comment", skip or it will be fixed by re-scrape
        if "comment" in title.lower() and len(title) < 15:
            continue

        magnet_tag = cells[2].find("a", href=lambda h: h and h.startswith("magnet:"))
        magnet = magnet_tag["href"] if magnet_tag else ""
        torrent_tag = cells[2].find("a", href=lambda h: h and h.endswith(".torrent"))
        torrent_url = BASE + torrent_tag["href"] if torrent_tag else ""
        dt = None
        if len(cells) > 4:
            dt = parse_dt(cells[4].get_text(strip=True))
        if dt is None:
            dt = datetime.now(timezone.utc)
        results.append({
            "tid": tid, "title": title,
            "link": f"{BASE}/view/{tid}",
            "magnet": magnet, "torrent_url": torrent_url,
            "pub_date": fmt_rfc2822(dt),
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
            tid_m = re.search(r"/view/(\d+)", guid.text)
            if not tid_m:
                continue
            tid = tid_m.group(1)
            
            title = item.findtext("title", "")
            # Discard bad entries so they can be re-scraped properly
            if "comment" in title.lower() and len(title) < 15:
                continue

            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            
            # Extract magnet from anywhere in the entry
            magnet = ""
            if link.startswith("magnet:"):
                magnet = link
            elif desc:
                m = re.search(r"(magnet:\?xt=urn:btih:[^\s<]+)", desc)
                if m:
                    magnet = m.group(1)
            
            torrent_url = f"{BASE}/download/{tid}.torrent"
            # If the link was a .torrent link, move it to torrent_url
            if link.endswith(".torrent"):
                torrent_url = link

            entries.append({
                "tid": tid, "title": title,
                "link": f"{BASE}/view/{tid}", # Internal reference link
                "magnet": magnet,
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
        
        # Use magnet in enclosure (highest priority for qBittorrent RSS parser)
        # and as <link> for other clients; .torrent URL stored in description
        magnet_url = e.get("magnet", "")
        torrent_url = e.get("torrent_url", f"{BASE}/download/{e['tid']}.torrent")
        
        ET.SubElement(item, "link").text = magnet_url if magnet_url else torrent_url
        
        g = ET.SubElement(item, "guid")
        g.set("isPermaLink", "true")
        g.text = f"{BASE}/view/{e['tid']}"
        ET.SubElement(item, "pubDate").text = e["pub_date"]
        
        ih = info_hash_from_magnet(magnet_url)
        if ih:
            ET.SubElement(item, f"{{{NS_NYAA}}}infoHash").text = ih
            
        desc = ET.SubElement(item, "description")
        if magnet_url and torrent_url:
            desc.text = f"{magnet_url}\nTorrent: {torrent_url}"
        elif magnet_url:
            desc.text = magnet_url
        else:
            desc.text = e["title"]
        
        enc = ET.SubElement(item, "enclosure")
        enc.set("url", magnet_url if magnet_url else torrent_url)
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

    merged = all_new + existing
    merged.sort(key=lambda e: date_sort_key(e["pub_date"]))
    print(f"Writing {len(merged)} entries to {feed_file}")
    with open(feed_file, "w", encoding="utf-8") as f:
        f.write(build_rss(merged, filter_text, feed_file, args.pages_url))


if __name__ == "__main__":
    main()
