#!/bin/bash
# Manual sitemap submission helpers
SITEMAP='https://yestubers.cloud/sitemap.xml'

echo "Google Search Console (manual required):"
echo "  1. Visit https://search.google.com/search-console"
echo "  2. Add property: https://yestubers.cloud"
echo "  3. Use HTML tag verification (already in site header)"
echo "  4. Submit sitemap: $SITEMAP"
echo

echo "Bing Webmaster Tools (manual required):"
echo "  1. Visit https://www.bing.com/webmasters"
echo "  2. Add site: https://yestubers.cloud"
echo "  3. Verify ownership"
echo "  4. Submit sitemap: $SITEMAP"
echo

# Google IndexNow is not supported directly by Google; use Bing API if you get an IndexNow key.
# echo "Pinging Google sitemap (deprecated endpoint, may return 404):"
# curl -s -o /dev/null -w "Google ping status: %{http_code}\n" "https://www.google.com/webmasters/sitemaps/ping?sitemap=$SITEMAP"
