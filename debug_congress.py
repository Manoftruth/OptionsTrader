import requests, zipfile, io, xml.etree.ElementTree as ET

headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"}

# We know DocID 20026590 = Pelosi Jan 2025 PTR
# Try fetching it as XML instead of PDF
test_doc_id = "20026590"
year = "2025"

test_urls = [
    f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{test_doc_id}.xml",
    f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{test_doc_id}.json",
    f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}/{test_doc_id}.xml",
    f"https://disclosures-clerk.house.gov/FinancialDisclosure/LoadDocument?fileId={test_doc_id}&type=ptr",
    f"https://disclosures-clerk.house.gov/api/filing/{test_doc_id}",
]

print("Testing individual filing endpoints...")
for url in test_urls:
    r = requests.get(url, headers=headers, timeout=10)
    print(f"[{r.status_code}] {url}")
    if r.status_code == 200:
        print(f"  Type: {r.headers.get('Content-Type')}")
        print(f"  Preview: {r.text[:300]}")

# Also: parse the 2025 index and count PTRs (FilingType=P)
print("\n\nParsing 2025 FD index for PTR filings...")
r = requests.get(
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2025FD.zip",
    headers=headers, timeout=15
)
z = zipfile.ZipFile(io.BytesIO(r.content))
xml_content = z.read("2025FD.xml")
root = ET.fromstring(xml_content)

ptrs = [m for m in root.findall("Member")
        if m.findtext("FilingType") == "P"]
print(f"Total PTR filings in 2025: {len(ptrs)}")
print("Sample PTRs:")
for m in ptrs[:5]:
    print(f"  {m.findtext('First')} {m.findtext('Last')} | "
          f"{m.findtext('FilingDate')} | DocID: {m.findtext('DocID')}")