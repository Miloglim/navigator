// Build-only tool: renders Nice Avatar into local PNGs; no browser runtime dependency.
const fs = require('node:fs');
const path = require('node:path');

const deps = process.env.AVATAR_BUILD_DEPS || path.resolve(__dirname, '../../../.work/avatar-build/node_modules');
const React = require(path.join(deps, 'react'));
const ReactDOMServer = require(path.join(deps, 'react-dom/server'));
const Avatar = require(path.join(deps, 'react-nice-avatar')).default;
const { chromium } = require(process.env.AVATAR_PLAYWRIGHT_PATH || require.resolve('playwright', { paths: [deps] }));

const output = path.resolve(__dirname, '../web/avatars');
const chrome = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const SIZE = 256;
const PEOPLE_PER_SEX = 5;
const LOOKS_PER_PERSON = 3;
const faces = ['#F6D3B9', '#F2C9AC', '#F8D9C2', '#EDBE9E', '#F5CDB0', '#E8B797'];
const hair = ['#26272C', '#342C2D', '#493633', '#222C32', '#563C35'];
const backgrounds = ['#D9E9E8', '#E1E6F3', '#E8E5F1', '#E5EDDF', '#F3E4E3', '#DFEAF1', '#EDE9D9', '#E9E2EC', '#DBECE6', '#F0E9E4'];
const shirts = ['#315B72', '#49677D', '#465E57', '#806078', '#A65F63', '#507787', '#91705C', '#5C6585', '#586E69', '#B07763'];
const eyes = ['circle', 'oval', 'smile'];
const mouths = ['smile', 'peace', 'laugh'];
const shirtStyles = ['polo', 'short', 'hoody'];

function config(sex, person, look) {
  const woman = sex === 'w';
  const index = (woman ? 0 : PEOPLE_PER_SEX) + person;
  const shirtColor = shirts[(index + look * 3) % shirts.length];
  return {
    shape: 'square', sex: woman ? 'woman' : 'man',
    faceColor: faces[(index * 5) % faces.length],
    hairColor: hair[(index * 3) % hair.length], hairColorRandom: false,
    hairStyle: woman ? ['womanLong', 'womanShort'][person % 2] : ['normal', 'thick', 'mohawk'][person % 3],
    hatStyle: look === 3 ? 'beanie' : 'none',
    hatColor: shirts[(index + 4) % shirts.length],
    glassesStyle: look === 2 ? (person % 2 ? 'round' : 'square') : 'none',
    earSize: index % 4 === 0 ? 'big' : 'small',
    eyeBrowStyle: woman ? 'upWoman' : 'up',
    eyeStyle: eyes[index % eyes.length],
    noseStyle: ['short', 'round', 'long'][(index * 2) % 3],
    mouthStyle: mouths[Math.floor(index / 3) % mouths.length],
    shirtStyle: look === 3 ? 'hoody' : shirtStyles[(person + look) % 2],
    shirtColor,
    bgColor: backgrounds[(index * 7) % backgrounds.length],
    isGradient: false,
  };
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath: chrome });
  try {
    const page = await browser.newPage({ viewport: { width: SIZE, height: SIZE }, deviceScaleFactor: 1 });
    for (const sex of ['w', 'm']) {
      for (let person = 1; person <= PEOPLE_PER_SEX; person++) {
        for (let look = 1; look <= LOOKS_PER_PERSON; look++) {
          const filename = `av2-${sex}${String(person).padStart(2, '0')}-${look}.png`;
          const markup = ReactDOMServer.renderToStaticMarkup(React.createElement(Avatar, {
            ...config(sex, person, look), id: 'avatar', style: { width: `${SIZE}px`, height: `${SIZE}px` },
          }));
          await page.setContent(`<!doctype html><html><body style="margin:0">${markup}</body></html>`);
          await page.locator('#avatar').screenshot({ path: path.join(output, filename), animations: 'disabled' });
        }
      }
    }
  } finally {
    await browser.close();
  }
  console.log(`Generated ${2 * PEOPLE_PER_SEX * LOOKS_PER_PERSON} grouped avatars in ${output}`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
