import requests
import re

resp = requests.post(
    'https://html.duckduckgo.com/html/',
    data={'q': 'Elon Musk site:linkedin.com', 'b': ''},
    headers={
        'User-Agent': 'hhgoa-task3/1.0',
        'Content-Type': 'application/x-www-form-urlencoded',
    },
    timeout=30
)
print('Status:', resp.status_code)
matches = re.findall(r'href="([^"]+)"', resp.text)
print('Total hrefs:', len(matches))
for m in matches[:20]:
    print('  ', m[:100])