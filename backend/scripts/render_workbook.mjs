import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputDir] = process.argv.slice(2);
if (!inputPath || !outputDir) {
  throw new Error("Usage: render_workbook.mjs INPUT.xlsx OUTPUT_DIR");
}

await fs.mkdir(outputDir, { recursive: true });
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const summary = await workbook.inspect({ kind: "sheet", include: "id,name" });
const records = String(summary?.ndjson ?? "").split("\n").filter(Boolean).map((line) => JSON.parse(line));
const sheets = records.filter((record) => record.kind === "sheet" || record.type === "sheet").map((record) => record.name ?? record.sheetName);

for (const entry of sheets) {
  const sheetName = entry.name ?? entry;
  const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  const safeName = String(sheetName).replaceAll(/[\\/:*?"<>|]/g, "_");
  await fs.writeFile(path.join(outputDir, `${safeName}.png`), new Uint8Array(await preview.arrayBuffer()));
}

console.log(JSON.stringify({ sheets, outputDir }, null, 2));
