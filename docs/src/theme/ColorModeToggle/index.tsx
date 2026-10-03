import React, {type ReactNode} from 'react';
import clsx from 'clsx';
import useIsBrowser from '@docusaurus/useIsBrowser';
import {translate} from '@docusaurus/Translate';
import {useColorMode} from '@docusaurus/theme-common';
import IconLightMode from '@theme/Icon/LightMode';
import IconDarkMode from '@theme/Icon/DarkMode';
import type {Props} from '@theme/ColorModeToggle';

import styles from './styles.module.css';

// Two modes only: light and dark. Until the visitor clicks, nothing is stored and the site
// follows the system setting (respectPrefersColorScheme in the config). A click then sets the
// opposite of the mode currently on screen, and that choice is remembered. The toggle never
// goes back to "system", unlike the stock three-state toggle.
function getModeLabel(mode: 'light' | 'dark'): string {
  return mode === 'dark'
    ? translate({
        message: 'Dark mode',
        id: 'theme.colorToggle.ariaLabel.mode.dark',
        description: 'The name for the dark color mode',
      })
    : translate({
        message: 'Light mode',
        id: 'theme.colorToggle.ariaLabel.mode.light',
        description: 'The name for the light color mode',
      });
}

function getAriaLabel(mode: 'light' | 'dark'): string {
  return translate(
    {
      message: 'Switch between dark and light mode (currently {mode})',
      id: 'theme.colorToggle.ariaLabel',
      description: 'The ARIA label for the color mode toggle',
    },
    {mode: getModeLabel(mode).toLowerCase()},
  );
}

function CurrentColorModeIcon(): ReactNode {
  // Both icons are always rendered; CSS shows the one for the mode on screen, which works
  // even before React hydrates because Docusaurus sets data-theme before first paint.
  return (
    <>
      <IconLightMode aria-hidden className={clsx(styles.toggleIcon, styles.lightToggleIcon)} />
      <IconDarkMode aria-hidden className={clsx(styles.toggleIcon, styles.darkToggleIcon)} />
    </>
  );
}

function ColorModeToggle({className, buttonClassName, onChange}: Props): ReactNode {
  const isBrowser = useIsBrowser();
  // The mode actually on screen: the saved choice, or the system setting when there is none.
  const {colorMode} = useColorMode();
  const next = colorMode === 'dark' ? 'light' : 'dark';
  return (
    <div className={clsx(styles.toggle, className)}>
      <button
        className={clsx(
          'clean-btn',
          styles.toggleButton,
          !isBrowser && styles.toggleButtonDisabled,
          buttonClassName,
        )}
        type="button"
        onClick={() => onChange(next)}
        disabled={!isBrowser}
        title={getModeLabel(colorMode)}
        aria-label={getAriaLabel(colorMode)}>
        <CurrentColorModeIcon />
      </button>
    </div>
  );
}

export default React.memo(ColorModeToggle);
