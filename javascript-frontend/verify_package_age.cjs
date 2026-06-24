#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const https = require("node:https");
const zlib = require("node:zlib");

const THRESHOLD_DAYS = 30;
const MAX_CONCURRENT_REQUESTS = 25; // Increased for better performance
const RETRY_DELAYS = [1, 3, 5, 10, 15]; // Backoff delays in seconds for 429s

const LOCK_FILE = path.join(process.cwd(), "package-lock.json");
const REPORT_FILE = path.join(process.cwd(), ".pkg-age-report.json");
const ERROR_LOG_FILE = path.join(process.cwd(), ".pkg-age-errors.json");

function daysSince(iso) {
  return Math.floor((Date.now() - new Date(iso)) / 86400000);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function httpsGet(url, retryCount = 0) {
  return new Promise((resolve, reject) => {
    https
      .get(
        url,
        {
          headers: {
            "User-Agent":
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.7727.56 Safari/537.36",
            "Accept-Encoding": "gzip, deflate",
          },
        },
        (res) => {
          // Handle 429 (Too Many Requests) with exponential backoff
          if (res.statusCode === 429) {
            res.resume(); // Consume the response
            if (retryCount < RETRY_DELAYS.length) {
              const delayMs = RETRY_DELAYS[retryCount] * 1000;
              console.log(
                `  ⏳ Rate limited (429). Retrying in ${RETRY_DELAYS[retryCount]}s (attempt ${retryCount + 1}/${RETRY_DELAYS.length})...`,
              );
              sleep(delayMs)
                .then(() => httpsGet(url, retryCount + 1))
                .then(resolve)
                .catch(reject);
            } else {
              reject(
                new Error(
                  `HTTP 429: Rate limited after ${RETRY_DELAYS.length} retries`,
                ),
              );
            }
            return;
          }

          // Only treat 404 as "not found" - other errors are transient or temporary
          if (res.statusCode === 404) {
            res.resume(); // Consume the response
            return reject(new Error(`HTTP 404: Package not found`));
          }

          // For other non-200 responses, retry a few times as they might be temporary
          if (res.statusCode !== 200) {
            res.resume(); // Consume the response
            if (retryCount < 2) {
              const delayMs = (retryCount + 1) * 500;
              sleep(delayMs)
                .then(() => httpsGet(url, retryCount + 1))
                .then(resolve)
                .catch(reject);
            } else {
              reject(new Error(`HTTP ${res.statusCode}: Temporary error`));
            }
            return;
          }

          let stream = res;
          const encoding = res.headers["content-encoding"];
          if (encoding === "gzip") {
            stream = res.pipe(zlib.createGunzip());
          } else if (encoding === "deflate") {
            stream = res.pipe(zlib.createInflate());
          }

          stream.on("error", (err) => reject(err));

          let data = "";
          stream.on("data", (chunk) => (data += chunk));
          stream.on("end", () => {
            try {
              resolve(JSON.parse(data));
            } catch (e) {
              reject(new Error(`Invalid JSON response: ${e.message}`));
            }
          });
        },
      )
      .on("error", reject);
  });
}

async function getPublishDate(name, version) {
  try {
    // Correctly escape scoped packages (e.g., @types/node -> @types%2Fnode)
    const escapedName = name.startsWith("@")
      ? `@${encodeURIComponent(name.slice(1))}`
      : encodeURIComponent(name);

    const data = await httpsGet(`https://registry.npmjs.org/${escapedName}`);
    return data.time && data.time[version] ? data.time[version] : null;
  } catch (error) {
    // Return null for truly not found packages (404), suppress logs for private packages
    if (error.message.includes("404")) {
      return null;
    }
    // For other errors (429, transient errors), log warning and return null to skip checking
    console.warn(
      `  ⚠️ Failed to fetch metadata for ${name}@${version}: ${error.message}`,
    );
    return null;
  }
}

function extractPackages(lock) {
  const out = {};

  // Support both package-lock v2 and v3 (uses lock.packages)
  if (lock.packages) {
    for (const [p, d] of Object.entries(lock.packages)) {
      if (!p || !d.version || d.link) continue;

      // Clean up package names from nested node_modules paths
      const name = p
        .replace(/^node_modules\//, "")
        .split("/node_modules/")
        .pop();
      if (name) out[name] = d.version;
    }
  }
  // Fallback support for older package-lock v1 formats
  else if (lock.dependencies) {
    for (const [name, d] of Object.entries(lock.dependencies)) {
      if (d.version) out[name] = d.version;
    }
  }
  return out;
}

// Helper utility to limit concurrency and optimize network traffic
async function asyncPool(poolLimit, array, iteratorFn) {
  const ret = [];
  const executing = new Set();
  for (const item of array) {
    const p = Promise.resolve().then(() => iteratorFn(item));
    ret.push(p);
    executing.add(p);
    const clean = () => executing.delete(p);
    p.then(clean, clean);
    if (executing.size >= poolLimit) {
      await Promise.race(executing);
    }
  }
  return Promise.all(ret);
}

async function main() {
  if (!fs.existsSync(LOCK_FILE)) {
    const errorMsg = `${LOCK_FILE} not found. Run 'npm install' or 'npm shrinkwrap' first.`;
    console.error(`❌ Error: ${errorMsg}`);

    // Write error for Slack
    fs.writeFileSync(
      ERROR_LOG_FILE,
      JSON.stringify(
        {
          success: false,
          error: errorMsg,
          generatedAt: new Date().toISOString(),
        },
        null,
        2,
      ),
    );

    process.exit(1);
  }

  console.log(`=== NPM Package Age Check (Threshold: ${THRESHOLD_DAYS}d) ===`);
  console.log(`Using ${MAX_CONCURRENT_REQUESTS} concurrent requests`);

  const lock = JSON.parse(fs.readFileSync(LOCK_FILE, "utf8"));
  const packages = extractPackages(lock);
  const packageEntries = Object.entries(packages);

  console.log(`Scanning ${packageEntries.length} dependencies...`);

  const results = {};
  const failed = [],
    passed = [],
    unknown = [],
    errors = [];

  // Execute registry queries with improved concurrency
  const startTime = Date.now();
  await asyncPool(
    MAX_CONCURRENT_REQUESTS,
    packageEntries,
    async ([name, version]) => {
      try {
        const pub = await getPublishDate(name, version);
        if (!pub) {
          unknown.push({ name, version });
          results[name] = { age: null, version };
          return;
        }
        const age = daysSince(pub);
        results[name] = { age, version };
        if (age < THRESHOLD_DAYS) {
          console.error(
            `  ❌ FAILED: ${name}@${version} — only ${age} day(s) old!`,
          );
          failed.push({ name, version, age });
        } else {
          passed.push({ name, version, age });
        }
      } catch (error) {
        errors.push({
          name,
          version,
          error: error.message,
        });
      }
    },
  );
  const duration = ((Date.now() - startTime) / 1000).toFixed(2);

  // Sort failures by age ascending (most recent first)
  failed.sort((a, b) => a.age - b.age);

  const summaryFile = process.env.GITHUB_STEP_SUMMARY;
  if (summaryFile) {
    const overallStatus = failed.length > 0 ? "FAILED" : "PASSED";
    let md = `## NPM Package Age Report\n`;
    md += `**Generated:** ${new Date().toISOString()}  |  **Threshold:** ${THRESHOLD_DAYS} days\n\n`;
    md += `**Overall Status:** ${overallStatus === "PASSED" ? "✅ PASSED" : "❌ FAILED"}\n\n`;
    md += `| Package | Status | Age |\n`;
    md += `| --- | --- | --- |\n`;

    const passRows = [],
      failRows = [];

    for (const [name, version] of packageEntries) {
      const r = results[name];
      if (!r) continue;
      if (r.age !== null && r.age < THRESHOLD_DAYS) {
        failRows.push(`| ${name}==${version} | ✖ FAIL | ${r.age} days |\n`);
      } else if (r.age !== null) {
        passRows.push(`| ${name}==${version} | ✔ PASS | ${r.age} days |\n`);
      } else {
        const isUnknown = unknown.some((u) => u.name === name);
        if (isUnknown) {
          failRows.push(`| ${name}==${version} | 🔒 N/A | — |\n`);
        } else {
          passRows.push(`| ${name}==${version} | ✔ PASS | — |\n`);
        }
      }
    }

    md += failRows.join("") + passRows.join("");

    md += `\n---\n`;
    md += `- 📦 **${packageEntries.length}** packages checked  |  ❌ **${failed.length}** failed (< ${THRESHOLD_DAYS}d)  |  🔒 **${unknown.length}** unavailable  |  ⚠ **${errors.length}** errors\n`;
    md += `- ⏱ Scan completed in **${duration}s**\n`;

    fs.appendFileSync(summaryFile, md + "\n");
  }

  // Write structural audit trace
  fs.writeFileSync(
    REPORT_FILE,
    JSON.stringify(
      {
        failed,
        passed,
        unknown,
        errors,
        duration: `${duration}s`,
        generatedAt: new Date().toISOString(),
      },
      null,
      2,
    ),
  );

  console.log("\n=== Summary ===");
  console.log(`✅ Scan completed in ${duration}s`);
  console.log(
    `  📊 ${packageEntries.length} packages checked | ${unknown.length} unavailable | ${errors.length} errors`,
  );

  if (failed.length) {
    const errorMsg = `${failed.length} package(s) are too new (< ${THRESHOLD_DAYS}d) to safely deploy:\n${failed.map((p) => `  - ${p.name}@${p.version} (${p.age} days old)`).join("\n")}`;
    console.error(`❌ FAILED: ${errorMsg}`);

    // Write error for Slack
    fs.writeFileSync(
      ERROR_LOG_FILE,
      JSON.stringify(
        {
          success: false,
          error: errorMsg,
          failed,
          generatedAt: new Date().toISOString(),
        },
        null,
        2,
      ),
    );

    process.exit(1);
  }

  console.log("✅ All public packages passed the maturity policy.");

  // Write success info for Slack
  fs.writeFileSync(
    ERROR_LOG_FILE,
    JSON.stringify(
      {
        success: true,
        generatedAt: new Date().toISOString(),
      },
      null,
      2,
    ),
  );
}

main().catch((e) => {
  console.error(`Unexpected execution error: ${e.message}`);

  // Write error for Slack
  fs.writeFileSync(
    ERROR_LOG_FILE,
    JSON.stringify(
      {
        success: false,
        error: e.message,
        generatedAt: new Date().toISOString(),
      },
      null,
      2,
    ),
  );

  process.exit(1);
});
