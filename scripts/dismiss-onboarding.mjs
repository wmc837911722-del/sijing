// Other feature suites explicitly skip first-run guidance through the UI.
// ux-onboarding.mjs exercises the tour itself without this helper.
export async function dismissOnboarding(page) {
  await page.locator('#bm-chat-input').waitFor();
  if (await page.locator('.tour-launcher').count()) {
    await page.waitForFunction(() => !document.querySelector('.tour-launcher')?.disabled);
    await page.waitForFunction(() => !document.querySelector('.guided-tour') || document.querySelector('.guided-tour').open);
    const guide = page.getByRole('dialog', { name: '使用引导', exact: true });
    if (await guide.isVisible()) await guide.getByRole('button', { name: '暂时跳过', exact: true }).click();
  }
}
