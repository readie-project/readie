import type {ReactNode} from 'react';
import {useNavbarMobileSidebar} from '@docusaurus/theme-common/internal';
import {translate} from '@docusaurus/Translate';
import IconClose from '@theme/Icon/Close';
import NavbarLogo from '@theme/Navbar/Logo';

// The header of the mobile sidebar: the logo and the close button. The stock header also holds
// a color mode toggle; Readie keeps that toggle in the navbar at every width instead
// (src/theme/ColorModeToggle/styles.module.css).
function CloseButton(): ReactNode {
  const mobileSidebar = useNavbarMobileSidebar();
  return (
    <button
      type="button"
      aria-label={translate({
        id: 'theme.docs.sidebar.closeSidebarButtonAriaLabel',
        message: 'Close navigation bar',
        description: 'The ARIA label for close button of mobile sidebar',
      })}
      className="clean-btn navbar-sidebar__close"
      onClick={() => mobileSidebar.toggle()}>
      <IconClose color="var(--ifm-color-emphasis-600)" />
    </button>
  );
}

export default function NavbarMobileSidebarHeader(): ReactNode {
  return (
    <div className="navbar-sidebar__brand">
      <NavbarLogo />
      <CloseButton />
    </div>
  );
}
