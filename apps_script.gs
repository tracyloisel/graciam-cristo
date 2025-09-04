
// Put this into Extensions → Apps Script in your daily sheet
const HEROKU_BASE = "https://YOUR-APP-NAME.herokuapp.com";

function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu("Illustrations")
    .addItem("Relancer génération (lignes sélectionnées)", "regenSelected")
    .addItem("Lancer tout le sheet (PENDING/ERROR/REGEN)", "runAll")
    .addToUi();
}

function regenSelected() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName("Prompts") || ss.getActiveSheet();
  const sel = sh.getActiveRange();
  if (!sel) { SpreadsheetApp.getUi().alert("Sélectionne au moins une ligne."); return; }
  const start = sel.getRow();
  const rows = [];
  for (let i = 0; i < sel.getNumRows(); i++) {
    rows.push(start + i);
  }
  const payload = {
    spreadsheetId: ss.getId(),
    rows: rows
  };
  UrlFetchApp.fetch(HEROKU_BASE + "/regenerate", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
  SpreadsheetApp.getUi().alert("Relance envoyée pour lignes: " + rows.join(", "));
}

function runAll() {
  const ss = SpreadsheetApp.getActive();
  const payload = { spreadsheetId: ss.getId() };
  UrlFetchApp.fetch(HEROKU_BASE + "/run", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
  SpreadsheetApp.getUi().alert("Génération lancée pour la feuille entière.");
}
