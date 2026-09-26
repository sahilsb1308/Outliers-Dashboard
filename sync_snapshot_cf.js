// Copy conditional formatting from Inventory Dashboard → Snapshot tab,
// rewriting column references to match Snapshot's column layout.
const https = require("https");
const { GoogleAuth } = require("google-auth-library");

const D2C_SHEET_ID  = "1ILrx79KdCV1-RDdwQPrrGsGyKe4s2698r3Mwcu9L18M";
const DASHBOARD_GID = 599219316;
const SNAPSHOT_GID  = 144053136;
const KEY_FILE      = "C:\\Users\\Sahil Gaur\\Documents\\Ops and Inventory\\service_account.json";

// Dashboard source cols → Snapshot dest cols (0-based indices)
const SRC_COLS = ["A","B","C","J","M","N","U","V","W","Y","AA","AB","AC","AE"];

function colIndex(letter) {
  return letter.split("").reduce((acc, c) => acc * 26 + c.charCodeAt(0) - 64, 0) - 1;
}
function colLetter(n) {
  let s = "";
  for (n++; n > 0; n = Math.floor((n-1)/26)) s = String.fromCharCode(((n-1)%26)+65) + s;
  return s;
}

// Build mapping: Dashboard col index → Snapshot col index
const colMap = new Map();
SRC_COLS.forEach((src, destIdx) => colMap.set(colIndex(src), destIdx));

// Rewrite a formula: replace Dashboard column refs with Snapshot column refs
function rewriteFormula(formula) {
  if (!formula) return formula;
  // Match $COL or COL references (e.g. $AA, AA) — replace known ones
  return formula.replace(/\$?([A-Z]{1,2})(?=\d)/g, (match, col) => {
    const srcIdx = colIndex(col);
    if (colMap.has(srcIdx)) {
      const destIdx = colMap.get(srcIdx);
      const hasAnchor = match.startsWith("$");
      return (hasAnchor ? "$" : "") + colLetter(destIdx);
    }
    return match; // leave unknown columns as-is
  });
}

// Rewrite a GridRange: remap column indices
function rewriteRange(range) {
  const r = { ...range, sheetId: SNAPSHOT_GID };
  if (r.startColumnIndex !== undefined) {
    const mapped = colMap.get(r.startColumnIndex);
    r.startColumnIndex = mapped !== undefined ? mapped : r.startColumnIndex;
  }
  if (r.endColumnIndex !== undefined) {
    const mapped = colMap.get(r.endColumnIndex - 1);
    r.endColumnIndex = mapped !== undefined ? mapped + 1 : r.endColumnIndex;
  }
  return r;
}

function post(url, body, token) {
  return new Promise((res, rej) => {
    const u = new URL(url);
    const req = https.request(
      { hostname: u.hostname, path: u.pathname + u.search, method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } },
      rs => { let d = ""; rs.on("data", c => d += c); rs.on("end", () => res(JSON.parse(d))); }
    );
    req.on("error", rej); req.write(body); req.end();
  });
}
function get(url, token) {
  return new Promise((res, rej) => {
    const u = new URL(url);
    https.get({ hostname: u.hostname, path: u.pathname + u.search, headers: { Authorization: `Bearer ${token}` } }, rs => {
      let d = ""; rs.on("data", c => d += c); rs.on("end", () => res(JSON.parse(d)));
    }).on("error", rej);
  });
}

async function main() {
  const auth = new GoogleAuth({ keyFile: KEY_FILE, scopes: ["https://www.googleapis.com/auth/spreadsheets"] });
  const { token } = await (await auth.getClient()).getAccessToken();

  // 1. Read CF rules from both sheets
  const meta = await get(
    `https://sheets.googleapis.com/v4/spreadsheets/${D2C_SHEET_ID}?fields=sheets(properties.sheetId,conditionalFormats)`,
    token
  );
  const dashboardSheet = meta.sheets.find(s => s.properties.sheetId === DASHBOARD_GID);
  const snapshotSheet  = meta.sheets.find(s => s.properties.sheetId === SNAPSHOT_GID);
  const dashRules  = dashboardSheet?.conditionalFormats ?? [];
  const snapRules  = snapshotSheet?.conditionalFormats ?? [];

  console.log(`Dashboard: ${dashRules.length} CF rules | Snapshot: ${snapRules.length} existing rules`);

  // 2. Delete all existing Snapshot CF rules (highest index first)
  const deleteRequests = snapRules.map((_, i) => snapRules.length - 1 - i)
    .map(index => ({ deleteConditionalFormatRule: { sheetId: SNAPSHOT_GID, index } }));

  // 3. Rewrite Dashboard CF rules for Snapshot column layout
  const addRequests = dashRules.map((rule, i) => {
    const adapted = JSON.parse(JSON.stringify(rule)); // deep clone

    // Rewrite ranges
    adapted.ranges = (adapted.ranges ?? []).map(rewriteRange);

    // Rewrite formula in booleanRule
    if (adapted.booleanRule?.condition?.values) {
      adapted.booleanRule.condition.values = adapted.booleanRule.condition.values.map(v =>
        v.userEnteredValue ? { ...v, userEnteredValue: rewriteFormula(v.userEnteredValue) } : v
      );
    }

    // Strip unsupported format properties — CF only allows color/bold/italic/strikethrough
    if (adapted.booleanRule?.format) {
      const { backgroundColor, backgroundColorStyle, textFormat } = adapted.booleanRule.format;
      adapted.booleanRule.format = {};
      if (backgroundColor)      adapted.booleanRule.format.backgroundColor      = backgroundColor;
      if (backgroundColorStyle) adapted.booleanRule.format.backgroundColorStyle = backgroundColorStyle;
      if (textFormat) {
        const { bold, italic, strikethrough, foregroundColor, foregroundColorStyle } = textFormat;
        adapted.booleanRule.format.textFormat = {};
        if (bold !== undefined)            adapted.booleanRule.format.textFormat.bold            = bold;
        if (italic !== undefined)          adapted.booleanRule.format.textFormat.italic          = italic;
        if (strikethrough !== undefined)   adapted.booleanRule.format.textFormat.strikethrough   = strikethrough;
        if (foregroundColor)               adapted.booleanRule.format.textFormat.foregroundColor = foregroundColor;
        if (foregroundColorStyle)          adapted.booleanRule.format.textFormat.foregroundColorStyle = foregroundColorStyle;
      }
    }

    return { addConditionalFormatRule: { rule: adapted, index: i } };
  });

  const requests = [...deleteRequests, ...addRequests];
  const res = await post(
    `https://sheets.googleapis.com/v4/spreadsheets/${D2C_SHEET_ID}:batchUpdate`,
    JSON.stringify({ requests }),
    token
  );
  if (res.error) throw new Error("Failed: " + JSON.stringify(res.error));
  console.log(`✓ Deleted ${deleteRequests.length} old rules, added ${addRequests.length} rewritten Dashboard rules to Snapshot`);

  // Show what was rewritten
  dashRules.forEach((rule, i) => {
    const vals = rule.booleanRule?.condition?.values ?? [];
    vals.forEach(v => {
      if (v.userEnteredValue) {
        const rewritten = rewriteFormula(v.userEnteredValue);
        if (rewritten !== v.userEnteredValue)
          console.log(`  Rule ${i+1}: "${v.userEnteredValue}" → "${rewritten}"`);
      }
    });
  });
}

main().catch(e => { console.error(e); process.exit(1); });
