// Replace snapshot tab static data with live column references from Inventory Dashboard
const https = require("https");
const { GoogleAuth } = require("google-auth-library");

const D2C_SHEET_ID   = "1ILrx79KdCV1-RDdwQPrrGsGyKe4s2698r3Mwcu9L18M";
const SNAPSHOT_TAB   = "Snapshot";
const DASHBOARD_TAB  = "Inventory Dashboard";
const KEY_FILE       = "C:\\Users\\Sahil Gaur\\Documents\\Ops and Inventory\\service_account.json";

// Dashboard source columns → snapshot destination columns (A=0, B=1, ...)
const COLUMN_MAP = ["A","B","C","J","M","N","U","V","W","Y","AA","AB","AC","AE"];

function colLetter(n) {
  let s = "";
  for (n++; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(((n - 1) % 26) + 65) + s;
  return s;
}
function httpsPost(url, body, headers) {
  return new Promise((res, rej) => {
    const u = new URL(url);
    const req = https.request({ hostname: u.hostname, path: u.pathname + u.search, method: "POST", headers }, rs => {
      let d = ""; rs.on("data", c => d += c); rs.on("end", () => res({ statusCode: rs.statusCode, body: d }));
    });
    req.on("error", rej); req.write(body); req.end();
  });
}

async function main() {
  const auth = new GoogleAuth({ keyFile: KEY_FILE, scopes: ["https://www.googleapis.com/auth/spreadsheets"] });
  const { token } = await (await auth.getClient()).getAccessToken();

  // 1. Clear the snapshot tab
  const clearRes = await httpsPost(
    `https://sheets.googleapis.com/v4/spreadsheets/${D2C_SHEET_ID}/values:batchClear`,
    JSON.stringify({ ranges: [`${SNAPSHOT_TAB}!A1:Z2000`] }),
    { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
  );
  if (JSON.parse(clearRes.body).error) throw new Error("Clear failed: " + clearRes.body);
  console.log("✓ Snapshot tab cleared");

  // 2. Write one array formula per snapshot column
  const data = COLUMN_MAP.map((srcCol, i) => ({
    range: `${SNAPSHOT_TAB}!${colLetter(i)}1`,
    values: [[`={'${DASHBOARD_TAB}'!${srcCol}:${srcCol}}`]]
  }));

  const writeRes = await httpsPost(
    `https://sheets.googleapis.com/v4/spreadsheets/${D2C_SHEET_ID}/values:batchUpdate`,
    JSON.stringify({ valueInputOption: "USER_ENTERED", data }),
    { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
  );
  if (JSON.parse(writeRes.body).error) throw new Error("Write failed: " + writeRes.body);
  console.log(`✓ ${COLUMN_MAP.length} live column references written to snapshot tab`);
  console.log("  Columns:", COLUMN_MAP.join(", "), "→ Snapshot A through", colLetter(COLUMN_MAP.length - 1));
}

main().catch(e => { console.error(e); process.exit(1); });
