"""Нарезает лист стикеров маскота на отдельные PNG-позы.

    docker run --rm -e HOME=/tmp -v $PWD:/srv -w /srv -u $(id -u):$(id -g) \
      --entrypoint python <образ> scripts/mascot_slice.py [лист.png]

Берёт первый PNG из assets/mascot/ (или указанный), находит стикеры по прозрачности и пишет
assets/mascot/poses/01.png, 02.png… (слева направо, сверху вниз) плюс контактный лист
data/mascot_contact.png с номерами — по нему выбирают, какая поза к какой сцене.
Pillow в образе нет, поэтому режем в Chromium через canvas: он там уже стоит ради рендера.
"""

import asyncio
import base64
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
POSE_H = 520  # высота позы на выходе: в ролике маскот ~300 px, запас на масштабирование

JS = """async ([b64, poseH]) => {
  const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
  const W = img.width, H = img.height;
  const c = document.createElement('canvas'); c.width = W; c.height = H;
  const x = c.getContext('2d', {willReadFrequently: true}); x.drawImage(img, 0, 0);
  const d = x.getImageData(0, 0, W, H).data;

  // плотные пиксели: слабое свечение вокруг стикеров не считается
  const M = new Uint8Array(W * H);
  for (let i = 0; i < W * H; i++) M[i] = d[i * 4 + 3] > 128 ? 1 : 0;

  // Стикеры касаются контурами, поэтому по связности их не разделить. Режем по сетке и границы
  // ищем в провалах плотности рядом с ожидаемыми местами (3 ряда по 6 поз, ниже ряд предметов).
  const valley = (lo, hi, density) => {
    let best = lo, bv = 1e18;
    for (let k = Math.max(0, lo); k < Math.min(density.length, hi); k++) if (density[k] < bv) { bv = density[k]; best = k; }
    return best;
  };
  const rowDen = new Float64Array(H);
  for (let y = 0; y < H; y++) { let n = 0; for (let x = 0; x < W; x++) n += M[y * W + x]; rowDen[y] = n; }
  const rowCuts = [0, ...[0.26, 0.50, 0.76].map(f => valley(Math.round(H * f) - 45, Math.round(H * f) + 45, rowDen)), 0];
  rowCuts[4] = H;

  const cells = [];
  for (let r = 0; r < 3; r++) {
    const y0 = rowCuts[r], y1 = rowCuts[r + 1];
    const colDen = new Float64Array(W);
    for (let x = 0; x < W; x++) { let n = 0; for (let y = y0; y < y1; y++) n += M[y * W + x]; colDen[x] = n; }
    const cuts = [0];
    for (let k = 1; k < 6; k++) cuts.push(valley(Math.round(W * k / 6) - 45, Math.round(W * k / 6) + 45, colDen));
    cuts.push(W);
    for (let c = 0; c < 6; c++) cells.push({x0: cuts[c], x1: cuts[c + 1], y0, y1});
  }

  // в ячейке оставляем крупные компоненты; обрывки соседних стикеров по краям отбрасываем
  const boxes = [];
  for (const cell of cells) {
    const cw = cell.x1 - cell.x0, ch = cell.y1 - cell.y0, lab = new Int32Array(cw * ch).fill(-1), comps = [];
    for (let i = 0; i < cw * ch; i++) {
      const gx = cell.x0 + (i % cw), gy = cell.y0 + ((i / cw) | 0);
      if (!M[gy * W + gx] || lab[i] >= 0) continue;
      const id = comps.length, st = [i]; lab[i] = id;
      let x0 = cw, y0 = ch, x1 = 0, y1 = 0, area = 0, edge = false;
      while (st.length) {
        const j = st.pop(), jx = j % cw, jy = (j / cw) | 0;
        area++; x0 = Math.min(x0, jx); x1 = Math.max(x1, jx); y0 = Math.min(y0, jy); y1 = Math.max(y1, jy);
        if (jx === 0 || jy === 0 || jx === cw - 1 || jy === ch - 1) edge = true;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          const nx = jx + dx, ny = jy + dy;
          if (nx < 0 || ny < 0 || nx >= cw || ny >= ch) continue;
          const n = ny * cw + nx;
          if (lab[n] < 0 && M[(cell.y0 + ny) * W + cell.x0 + nx]) { lab[n] = id; st.push(n); }
        }
      }
      comps.push({x0, y0, x1, y1, area, edge, id});
    }
    const maxA = Math.max(1, ...comps.map(k => k.area));
    const keep = comps.filter(k => k.area > maxA * 0.04 && !(k.edge && k.area < maxA * 0.25));
    if (!keep.length) continue;
    const bx0 = Math.min(...keep.map(k => k.x0)), by0 = Math.min(...keep.map(k => k.y0));
    const bx1 = Math.max(...keep.map(k => k.x1)), by1 = Math.max(...keep.map(k => k.y1));
    const keepIds = new Set(keep.map(k => k.id));
    boxes.push({cell, lab, cw, keepIds, x0: bx0, y0: by0, x1: bx1, y1: by1});
  }

  const out = [];
  const pad = 6;
  for (const b of boxes) {
    const sx = b.cell.x0 + b.x0 - pad, sy = b.cell.y0 + b.y0 - pad;
    const sw = b.x1 - b.x0 + 1 + pad * 2, sh = b.y1 - b.y0 + 1 + pad * 2;
    const cc = document.createElement('canvas'); cc.width = sw; cc.height = sh;
    const cx = cc.getContext('2d', {willReadFrequently: true});
    cx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    const id = cx.getImageData(0, 0, sw, sh), p = id.data;
    for (let yy = 0; yy < sh; yy++) for (let xx = 0; xx < sw; xx++) {
      const gx = sx + xx, gy = sy + yy, o = (yy * sw + xx) * 4 + 3;
      const lx = gx - b.cell.x0, ly = gy - b.cell.y0;
      const inCell = lx >= 0 && ly >= 0 && lx < b.cw && ly < b.cell.y1 - b.cell.y0;
      // чужие пиксели (соседний стикер) и слабое свечение (alpha < 40) — в прозрачность
      if (!inCell || p[o] < 40 || (M[gy * W + gx] && !b.keepIds.has(b.lab[ly * b.cw + lx]))) p[o] = 0;
    }
    cx.putImageData(id, 0, 0);
    const k = poseH / sh, oc = document.createElement('canvas');
    oc.width = Math.round(sw * k); oc.height = poseH;
    const ox = oc.getContext('2d'); ox.imageSmoothingQuality = 'high'; ox.drawImage(cc, 0, 0, oc.width, oc.height);
    out.push({png: oc.toDataURL('image/png').split(',')[1], w: oc.width, h: poseH,
              box: [sx, sy, sw, sh]});
  }

  // контактный лист с номерами
  const cols = 8, cell = 220, rowsN = Math.ceil(out.length / cols);
  const sheet = document.createElement('canvas'); sheet.width = cols * cell; sheet.height = rowsN * cell;
  const sx2 = sheet.getContext('2d'); sx2.fillStyle = '#20242b'; sx2.fillRect(0, 0, sheet.width, sheet.height);
  for (let i = 0; i < out.length; i++) {
    const im2 = new Image(); im2.src = 'data:image/png;base64,' + out[i].png; await im2.decode();
    const s = Math.min((cell - 20) / im2.width, (cell - 40) / im2.height);
    const cx0 = (i % cols) * cell, cy0 = Math.floor(i / cols) * cell;
    sx2.drawImage(im2, cx0 + (cell - im2.width * s) / 2, cy0 + 30, im2.width * s, im2.height * s);
    sx2.fillStyle = '#fff'; sx2.font = 'bold 24px sans-serif'; sx2.fillText(String(i + 1).padStart(2, '0'), cx0 + 8, cy0 + 26);
  }
  return {poses: out, sheet: sheet.toDataURL('image/png').split(',')[1], total: boxes.length};
}"""


async def main() -> None:
    mascot_dir = ROOT / "assets" / "mascot"
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else next(
        p for p in sorted(mascot_dir.glob("*.png")) if p.name not in ("mascot.png", "mascot_talk.png"))
    out_dir = mascot_dir / "poses"
    out_dir.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page()
        res = await page.evaluate(JS, [base64.b64encode(src.read_bytes()).decode(), POSE_H])
        await browser.close()
    for old in out_dir.glob("*.png"):
        old.unlink()
    for i, p in enumerate(res["poses"], 1):
        (out_dir / f"{i:02d}.png").write_bytes(base64.b64decode(p["png"]))
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "mascot_contact.png").write_bytes(base64.b64decode(res["sheet"]))
    print(f"{src.name}: компонентов {res['total']}, поз {len(res['poses'])} → {out_dir}")


asyncio.run(main())
