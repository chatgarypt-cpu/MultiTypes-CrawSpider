"""Channel registry: add a file here and one line below to support a new discovery rule."""
from crawler.channels import wp_api, sitemap, rss, html_list, json_api, json_list, archive

REGISTRY = {
    "wp-api":    wp_api.run,
    "sitemap":   sitemap.run,
    "rss":       rss.run,
    "html":      html_list.run,
    "json-api":  json_api.run,
    "json-list": json_list.run,
    "archive":   archive.run,
}
