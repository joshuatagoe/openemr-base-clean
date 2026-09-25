/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot source viewer overlay (ADR-008): the 0-1, top-left,
 * cropbox-relative bbox in the displayed (rotation-applied) frame of the page
 * (photos: the EXIF-oriented image) becomes a pixel rectangle over the
 * rendered page. Pure functions, exported by the panel script for tests.
 *
 * Run with: npx jest tests/js/copilot-overlay.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { overlayRect, pdfFrame, imageFrame } = lib;

function close(rect, expected) {
    expect(rect).not.toBeNull();
    ['left', 'top', 'width', 'height'].forEach((k) => {
        expect(rect[k]).toBeCloseTo(expected[k], 6);
    });
}

describe('overlayRect - PDF pages rendered by pdf.js', () => {
    test('plain page: the box scales to the rendered viewport', () => {
        // US Letter at scale 1.5: 918 x 1188 CSS px.
        const frame = pdfFrame({ width: 918, height: 1188, rotation: 0 }, 0);
        close(overlayRect([0.1, 0.2, 0.3, 0.25], frame), { left: 91.8, top: 237.6, width: 183.6, height: 59.4 });
    });

    test('cropbox smaller than mediabox: no mediabox offset is added', () => {
        // pdf.js viewports are built from the cropbox (viewBox); the box is cropbox-relative,
        // so the cropbox's 50 pt offset inside the mediabox must not shift the rectangle.
        const viewport = { width: 512, height: 692, rotation: 0, viewBox: [50, 50, 562, 742] };
        close(overlayRect([0, 0, 0.5, 0.5], pdfFrame(viewport, 0)), { left: 0, top: 0, width: 256, height: 346 });
    });

    test.each([90, 180, 270])('page with /Rotate %i rendered at its own rotation: box is already in the displayed frame', (rot) => {
        // The agent's boxes are in the displayed (rotation-applied) frame; pdf.js renders the page
        // at its own /Rotate by default, so only scaling applies. Width/height swap for 90/270.
        const swapped = rot % 180 !== 0;
        const viewport = { width: swapped ? 792 : 612, height: swapped ? 612 : 792, rotation: rot };
        const rect = overlayRect([0.25, 0.5, 0.75, 0.625], pdfFrame(viewport, rot));
        close(rect, { left: 0.25 * viewport.width, top: 0.5 * viewport.height, width: 0.5 * viewport.width, height: 0.125 * viewport.height });
    });

    test('extra rotation of 90 degrees clockwise beyond the page\'s own rotation', () => {
        // Displayed frame 100 x 200 rotated 90 clockwise -> 200 x 100 on screen.
        // Point (x, y) -> (1 - y, x). Box x 0.1-0.3, y 0.2-0.25 -> x' 0.75-0.8, y' 0.1-0.3.
        const frame = pdfFrame({ width: 200, height: 100, rotation: 90 }, 0);
        close(overlayRect([0.1, 0.2, 0.3, 0.25], frame), { left: 150, top: 10, width: 10, height: 20 });
    });

    test('extra rotation of 180 degrees', () => {
        const frame = pdfFrame({ width: 100, height: 200, rotation: 270 }, 90);
        // (x, y) -> (1 - x, 1 - y): x' 0.7-0.9, y' 0.75-0.8
        close(overlayRect([0.1, 0.2, 0.3, 0.25], frame), { left: 70, top: 150, width: 20, height: 10 });
    });

    test('extra rotation of 270 degrees', () => {
        const frame = pdfFrame({ width: 200, height: 100, rotation: 270 }, 0);
        // (x, y) -> (y, 1 - x): x' 0.2-0.25, y' 0.7-0.9
        close(overlayRect([0.1, 0.2, 0.3, 0.25], frame), { left: 40, top: 70, width: 10, height: 20 });
    });
});

describe('overlayRect - photos shown in <img>', () => {
    test('PNG: the box scales to the displayed image size', () => {
        const frame = imageFrame({ clientWidth: 640, clientHeight: 480 });
        close(overlayRect([0.5, 0.5, 1, 1], frame), { left: 320, top: 240, width: 320, height: 240 });
    });

    test('EXIF-rotated JPEG: the browser shows it upright, and the box is in that oriented frame', () => {
        // Stored 4000 x 3000 with EXIF orientation 6; browsers apply image-orientation: from-image,
        // so the element is portrait. The agent EXIF-oriented the photo before OCR, so only scaling applies.
        const img = { naturalWidth: 3000, naturalHeight: 4000, clientWidth: 300, clientHeight: 400 };
        close(overlayRect([0.1, 0.2, 0.3, 0.25], imageFrame(img)), { left: 30, top: 80, width: 60, height: 20 });
    });
});

describe('overlayRect - no guessed boxes', () => {
    const frame = { width: 100, height: 100, rotation: 0 };

    test.each([
        ['null', null],
        ['undefined', undefined],
        ['too short', [0.1, 0.2, 0.3]],
        ['not numbers', ['0.1', 0.2, 0.3, 0.4]],
        ['outside 0-1', [0.1, 0.2, 1.3, 0.4]],
        ['negative', [-0.1, 0.2, 0.3, 0.4]],
        ['inverted', [0.3, 0.2, 0.1, 0.4]],
        ['zero area', [0.1, 0.2, 0.1, 0.4]],
        ['NaN', [0.1, NaN, 0.3, 0.4]],
    ])('%s bbox -> null (the viewer shows the notice, never a box)', (_label, bbox) => {
        expect(overlayRect(bbox, frame)).toBeNull();
    });

    test('an unrendered frame -> null', () => {
        expect(overlayRect([0.1, 0.2, 0.3, 0.4], { width: 0, height: 0 })).toBeNull();
        expect(overlayRect([0.1, 0.2, 0.3, 0.4], null)).toBeNull();
    });

    test('an unknown extra rotation -> null', () => {
        expect(overlayRect([0.1, 0.2, 0.3, 0.4], { width: 10, height: 10, rotation: 45 })).toBeNull();
    });
});

describe('frames', () => {
    test('pdfFrame: extra rotation is the viewport rotation minus the page\'s own, normalised', () => {
        expect(pdfFrame({ width: 10, height: 20, rotation: 0 }, 270)).toEqual({ width: 10, height: 20, rotation: 90 });
        expect(pdfFrame({ width: 10, height: 20, rotation: 90 }, 90)).toEqual({ width: 10, height: 20, rotation: 0 });
    });

    test('imageFrame uses the displayed size, never the natural size', () => {
        expect(imageFrame({ naturalWidth: 4000, naturalHeight: 3000, clientWidth: 400, clientHeight: 300 })).toEqual({ width: 400, height: 300, rotation: 0 });
    });
});
