import os
from playwright.sync_api import expect, sync_playwright

URL = os.getenv('PUBLIC_URL', 'http://localhost:8787')

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox', '--disable-dev-shm-usage'])
    for width, height in [(1280, 900), (390, 844)]:
        page = browser.new_page(viewport={'width': width, 'height': height})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(URL)
        expect(page.get_by_role('heading', name='Cine en casa')).to_be_visible()
        expect(page.get_by_text('solo puedes consultar cómo va la sesión')).to_be_visible()
        expect(page.locator('input, button, select, video')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors, errors
        print('PASS browser informational-only/no controls/no overflow', width, height)
        page.close()
    browser.close()
