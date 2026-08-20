import fs from 'fs';
import path from 'path';
import zlib from 'zlib';

function createPNG(width, height, drawFn) {
  // RGBA buffer with filter byte at start of each scanline
  const stride = width * 4 + 1;
  const rawData = Buffer.alloc(stride * height);

  for (let y = 0; y < height; y++) {
    const rowOffset = y * stride;
    rawData[rowOffset] = 0; // Filter type 0 (None)
    for (let x = 0; x < width; x++) {
      const pixelOffset = rowOffset + 1 + x * 4;
      const [r, g, b, a] = drawFn(x, y, width, height);
      rawData[pixelOffset] = Math.max(0, Math.min(255, Math.round(r)));
      rawData[pixelOffset + 1] = Math.max(0, Math.min(255, Math.round(g)));
      rawData[pixelOffset + 2] = Math.max(0, Math.min(255, Math.round(b)));
      rawData[pixelOffset + 3] = Math.max(0, Math.min(255, Math.round(a)));
    }
  }

  const deflated = zlib.deflateSync(rawData, { level: 9 });

  // PNG Signature
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

  // IHDR chunk
  const ihdrData = Buffer.alloc(13);
  ihdrData.writeUInt32BE(width, 0);
  ihdrData.writeUInt32BE(height, 4);
  ihdrData[8] = 8; // 8 bit depth
  ihdrData[9] = 6; // Color type 6 (RGBA)
  ihdrData[10] = 0; // Compression
  ihdrData[11] = 0; // Filter
  ihdrData[12] = 0; // Interlace
  const ihdrChunk = createChunk('IHDR', ihdrData);

  // IDAT chunk
  const idatChunk = createChunk('IDAT', deflated);

  // IEND chunk
  const iendChunk = createChunk('IEND', Buffer.alloc(0));

  return Buffer.concat([signature, ihdrChunk, idatChunk, iendChunk]);
}

function createChunk(type, data) {
  const length = data.length;
  const chunk = Buffer.alloc(8 + length + 4);
  chunk.writeUInt32BE(length, 0);
  chunk.write(type, 4, 4, 'ascii');
  data.copy(chunk, 8);
  const crc = crc32(chunk.subarray(4, 8 + length));
  chunk.writeInt32BE(crc, 8 + length);
  return chunk;
}

// CRC32 table
const crcTable = new Int32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) {
    c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
  }
  crcTable[n] = c;
}

function crc32(buf) {
  let crc = -1;
  for (let i = 0; i < buf.length; i++) {
    crc = (crc >>> 8) ^ crcTable[(crc ^ buf[i]) & 0xFF];
  }
  return (crc ^ -1) | 0;
}

// Icon Drawer: Professional Modern Trading Icon
// Features: Dark blue-black rounded aesthetic with glowing cyan/blue/emerald bull chart & candlestick elements
function drawTradingIcon(x, y, w, h, isMaskable = false) {
  const nx = x / w; // 0 to 1
  const ny = y / h; // 0 to 1
  const cx = 0.5;
  const cy = 0.5;

  // Background rounded square or maskable full-bleed
  let bgR = 13, bgG = 20, bgB = 36, bgA = 255; // #0d1424
  const distCenter = Math.sqrt((nx - cx) ** 2 + (ny - cy) ** 2);

  // Radial gradient in background
  const glow = Math.max(0, 1 - distCenter * 1.8);
  bgR = Math.min(255, bgR + glow * 20);
  bgG = Math.min(255, bgG + glow * 35);
  bgB = Math.min(255, bgB + glow * 70);

  // If not maskable, round corners with anti-aliasing
  if (!isMaskable) {
    const cornerRadius = 0.22;
    const dx = Math.max(0, Math.abs(nx - 0.5) - (0.5 - cornerRadius));
    const dy = Math.max(0, Math.abs(ny - 0.5) - (0.5 - cornerRadius));
    const cornerDist = Math.sqrt(dx * dx + dy * dy);
    if (cornerDist > cornerRadius) {
      const edge = (cornerDist - cornerRadius) * w;
      if (edge >= 1.5) return [0, 0, 0, 0];
      bgA = Math.round(255 * (1 - edge / 1.5));
    }
  }

  // Draw Foreground Elements:
  // Scale down for maskable to stay within safe zone (center 66%)
  const scale = isMaskable ? 0.72 : 0.88;
  const px = (nx - 0.5) / scale + 0.5;
  const py = (ny - 0.5) / scale + 0.5;

  // Outer border accent ring
  if (!isMaskable && distCenter > 0.44 && distCenter < 0.47) {
    return [37, 99, 235, Math.min(255, bgA * 0.8)]; // Blue border ring
  }

  // Draw Candlesticks and Trend Line if inside 0.15 <= px, py <= 0.85
  if (px >= 0.12 && px <= 0.88 && py >= 0.12 && py <= 0.88) {
    // 3 Candlestick bars:
    // Bar 1 (Left - Bullish green): x: 0.26 - 0.34, Wick: 0.29-0.31 (y: 0.45 to 0.78), Body: y: 0.52 to 0.72
    // Bar 2 (Mid - Pullback blue): x: 0.46 - 0.54, Wick: 0.49-0.51 (y: 0.40 to 0.70), Body: y: 0.45 to 0.62
    // Bar 3 (Right - Massive breakout cyan/emerald): x: 0.66 - 0.74, Wick: 0.69-0.71 (y: 0.22 to 0.55), Body: y: 0.26 to 0.48

    // Bar 1:
    if (Math.abs(px - 0.30) < 0.012 && py >= 0.48 && py <= 0.78) {
      return [16, 185, 129, bgA]; // emerald wick
    }
    if (px >= 0.25 && px <= 0.35 && py >= 0.54 && py <= 0.72) {
      return [16, 185, 129, bgA]; // emerald body
    }

    // Bar 2:
    if (Math.abs(px - 0.50) < 0.012 && py >= 0.38 && py <= 0.68) {
      return [59, 130, 246, bgA]; // blue wick
    }
    if (px >= 0.45 && px <= 0.55 && py >= 0.44 && py <= 0.60) {
      return [59, 130, 246, bgA]; // blue body
    }

    // Bar 3 (Breakout):
    if (Math.abs(px - 0.70) < 0.012 && py >= 0.20 && py <= 0.56) {
      return [6, 182, 212, bgA]; // cyan wick
    }
    if (px >= 0.65 && px <= 0.75 && py >= 0.24 && py <= 0.48) {
      return [6, 182, 212, bgA]; // cyan body
    }

    // Dynamic upward trend line (spline connecting bottoms to top breakout)
    // Formula: y(px) = 0.75 - 0.7 * (px - 0.15)^1.2
    const targetY = 0.78 - 0.68 * Math.pow(Math.max(0, px - 0.18) / 0.62, 1.1);
    const lineDist = Math.abs(py - targetY);
    if (px >= 0.20 && px <= 0.82 && lineDist < 0.024) {
      const alpha = Math.max(0, 1 - lineDist / 0.024);
      return [
        Math.round(56 * alpha + bgR * (1 - alpha)),
        Math.round(189 * alpha + bgG * (1 - alpha)),
        Math.round(248 * alpha + bgB * (1 - alpha)), // Sky blue line
        bgA,
      ];
    }

    // Arrow tip at (0.80, 0.22)
    if (px >= 0.76 && px <= 0.83 && py >= 0.18 && py <= 0.28) {
      const arrowDx = px - 0.80;
      const arrowDy = py - 0.22;
      if (Math.abs(arrowDx + arrowDy) < 0.035 && Math.abs(arrowDx - arrowDy) < 0.035) {
        return [56, 189, 248, bgA];
      }
    }
  }

  return [bgR, bgG, bgB, bgA];
}

// Generate all icons
const outDir = path.resolve('public/icons');
fs.mkdirSync(outDir, { recursive: true });

console.log('Generating PWA Icons...');

const icons = [
  { file: 'icon-192.png', size: 192, maskable: false },
  { file: 'icon-512.png', size: 512, maskable: false },
  { file: 'icon-512-maskable.png', size: 512, maskable: true },
  { file: 'apple-touch-icon.png', size: 180, maskable: false },
  { file: 'favicon-32x32.png', size: 32, maskable: false },
  { file: 'favicon-16x16.png', size: 16, maskable: false },
];

for (const icon of icons) {
  const pngBuf = createPNG(icon.size, icon.size, (x, y, w, h) =>
    drawTradingIcon(x, y, w, h, icon.maskable)
  );
  fs.writeFileSync(path.join(outDir, icon.file), pngBuf);
  console.log(`✓ Generated ${icon.file} (${icon.size}x${icon.size})`);
}

// Also write vector SVG
const svgContent = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0d1424" />
      <stop offset="50%" stop-color="#141f36" />
      <stop offset="100%" stop-color="#0a0e1a" />
    </linearGradient>
    <linearGradient id="bullGrad" x1="0%" y1="100%" x2="0%" y2="0%">
      <stop offset="0%" stop-color="#10b981" />
      <stop offset="100%" stop-color="#06b6d4" />
    </linearGradient>
    <linearGradient id="trendGrad" x1="0%" y1="100%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#3b82f6" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="8" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>
  <!-- Background with rounded corners -->
  <rect width="512" height="512" rx="112" fill="url(#bgGrad)" stroke="#1e2d45" stroke-width="4" />
  
  <!-- Subtle Grid lines -->
  <line x1="80" y1="180" x2="432" y2="180" stroke="#1e2d45" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.6"/>
  <line x1="80" y1="280" x2="432" y2="280" stroke="#1e2d45" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.6"/>
  <line x1="80" y1="380" x2="432" y2="380" stroke="#1e2d45" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.6"/>

  <!-- Candlestick 1: Bullish Entry -->
  <line x1="150" y1="240" x2="150" y2="400" stroke="#10b981" stroke-width="4" stroke-linecap="round"/>
  <rect x="130" y="275" width="40" height="95" rx="6" fill="#10b981" />

  <!-- Candlestick 2: Momentum Continuation -->
  <line x1="256" y1="190" x2="256" y2="350" stroke="#3b82f6" stroke-width="4" stroke-linecap="round"/>
  <rect x="236" y="225" width="40" height="85" rx="6" fill="#3b82f6" />

  <!-- Candlestick 3: Breakout High -->
  <line x1="362" y1="100" x2="362" y2="290" stroke="#06b6d4" stroke-width="4" stroke-linecap="round"/>
  <rect x="342" y="125" width="40" height="120" rx="6" fill="url(#bullGrad)" filter="url(#glow)"/>

  <!-- Trend Curve with Glow -->
  <path d="M 100 390 Q 240 360 330 200 T 420 110" fill="none" stroke="url(#trendGrad)" stroke-width="7" stroke-linecap="round" filter="url(#glow)"/>
  
  <!-- Trend Indicator Arrow -->
  <polygon points="425,105 400,120 415,135" fill="#38bdf8" />
  <circle cx="420" cy="110" r="5" fill="#ffffff" />
</svg>`;

fs.writeFileSync(path.join(outDir, 'icon.svg'), svgContent);
fs.writeFileSync(path.resolve('public/favicon.svg'), svgContent);
console.log('✓ Generated SVG icons');
