import requests
from urllib.parse import urlparse
import mimetypes
import pandas as pd
import os
from tqdm import tqdm
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- FUNCTIONS ---

MAX_THREADS = 20    

def run_graphql_query(query, variables, github_token):
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json"
    }
    response = requests.post(
        "https://api.github.com/graphql",
        headers=headers,
        json={"query": query, "variables": variables or {}}
    )
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise Exception(data["errors"])
    return data["data"]


def get_pull_requests_paginated(repo, github_token):
    """Generator that yields PRs page by page instead of all at once."""
    owner, name = repo.split("/")
    cursor = None
    has_next_page = True

    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(first: 50, after: $cursor, states: [OPEN, MERGED, CLOSED]) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            number
            title
            body
          }
        }
      }
    }
    """

    while has_next_page:
        variables = {"owner": owner, "name": name, "cursor": cursor}
        data = run_graphql_query(query, variables, github_token)
        pr_data = data["repository"]["pullRequests"]
        yield pr_data["nodes"]  # yield one page at a time
        has_next_page = pr_data["pageInfo"]["hasNextPage"]
        cursor = pr_data["pageInfo"]["endCursor"]


def extract_visible_http_links(markdown_text):
    """Extract http/https links directly from markdown text (faster & low memory)."""
    if not markdown_text:
        return []
    # Simple regex for http/https URLs
    link_pattern = re.compile(r'https?://[^\s<>()\[\]`]+')
    return link_pattern.findall(markdown_text)


def get_media_type(url):
    # --- sanitize malformed URLs first ---
    url = url.strip().strip('`[]()<>')

    # ignore localhost or malformed URLs
    if "localhost" in url or "127.0.0.1" in url or not re.match(r"^https?://", url):
        return "invalid"

    try:
        response = requests.head(url, allow_redirects=True, timeout=5)
        content_type = response.headers.get("Content-Type", "")
        if content_type:
            if "image" in content_type:
                return "image"
            elif "audio" in content_type:
                return "audio"
            elif "video" in content_type:
                return "video"
            elif "text" in content_type or "html" in content_type:
                return "text"
            else:
                return content_type.split("/")[0]
    except requests.exceptions.InvalidURL:
        return "invalid"
    except Exception:
        # fallback to extension-based guess
        try:
            ext = urlparse(url).path.split(".")[-1]
            guessed_type = mimetypes.guess_type(f"file.{ext}")[0]
            if guessed_type:
                return guessed_type.split("/")[0]
        except Exception:
            return "unknown"

        return "unknown"

    return "unknown"

def extract_pr_links(repo, github_token):
    repo_name = repo.replace("/", "_")
    csv_file = f"{repo_name}.csv"

    if not os.path.exists(csv_file):
        pd.DataFrame(columns=["repo", "pr_link", "pr_title", "link", "media_type", "isGithub"]).to_csv(
            csv_file, index=False
        )

    total_processed = 0
    all_results = []

    print(f"🔍 Fetching PRs for {repo}...")

    for pr_page in tqdm(get_pull_requests_paginated(repo, github_token), desc="Fetching PR pages"):
        page_results = []
        links_to_check = []

        for pr in pr_page:
            pr_number = pr["number"]
            pr_title = pr["title"]
            pr_body = pr["body"] or ""
            pr_link = f"https://github.com/{repo}/pull/{pr_number}"

            links = extract_visible_http_links(pr_body)
            for link in links:
                links_to_check.append((pr_link, pr_title, link))

        # Parallel processing of links
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            future_to_link = {
                executor.submit(get_media_type, link): (pr_link, pr_title, link)
                for (pr_link, pr_title, link) in links_to_check
            }

            for future in tqdm(as_completed(future_to_link), total=len(future_to_link), desc="Checking links"):
                pr_link, pr_title, link = future_to_link[future]
                try:
                    media_type = future.result()
                except Exception:
                    media_type = "unknown"

                is_github = link.startswith("https://github.com")
                page_results.append({
                    "repo": repo,
                    "pr_link": pr_link,
                    "pr_title": pr_title,
                    "link": link,
                    "media_type": media_type,
                    "isGithub": is_github
                })

        if page_results:
            df = pd.DataFrame(page_results)
            df.to_csv(csv_file, mode="a", header=False, index=False)
            total_processed += len(page_results)
            tqdm.write(f"💾 Saved {len(page_results)} links (total so far: {total_processed})")

    print(f"✅ Finished! Total links saved for {repo}: {total_processed} → {csv_file}")
    
# --- MAIN ---
if __name__ == "__main__":
    repo = input("Enter the repo name (e.g. owner/repo): ").strip()
    github_token = input("Enter your GitHub token: ").strip()
    extract_pr_links(repo, github_token)
