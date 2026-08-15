/**
 * OK Rafting — calendar sync trigger.
 *
 * Runs as a Cloudflare Worker with a Cron Trigger (every 15 min, off-grid
 * minutes) and tells GitHub to run the "Sync calendar" workflow via the
 * repository_dispatch API. This replaces GitHub's own schedule trigger,
 * which was observed dropping runs for hours at a time (2026).
 *
 * Setup: see worker/SETUP.md — the GitHub PAT lives in the GH_PAT secret.
 */

export default {
  async scheduled(event, env, ctx) {
    const res = await fetch(
      "https://api.github.com/repos/vladosam/OK-Rafting/dispatches",
      {
        method: "POST",
        headers: {
          // GH_PAT = fine-grained PAT, Contents: Read and write, only this repo
          Authorization: `Bearer ${env.GH_PAT}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "ok-rafting-sync-trigger",
        },
        body: JSON.stringify({ event_type: "sync-calendar" }),
      }
    );

    // GitHub returns 204 No Content on success.
    if (res.status !== 204) {
      const body = await res.text();
      console.error(`dispatch failed: HTTP ${res.status} ${body.slice(0, 300)}`);
      throw new Error(`dispatch failed: HTTP ${res.status}`);
    }
    console.log(`OK: dispatch sent at ${new Date(event.scheduledTime).toISOString()}`);
  },
};
