import React, {type ReactNode} from 'react';
import clsx from 'clsx';
import {useThemeConfig} from '@docusaurus/theme-common';
import FooterLinkItem from '@theme/Footer/LinkItem';
import useServiceStatus, {type ServiceStatus} from '../../hooks/useServiceStatus';

import styles from './styles.module.css';

// A slim, single-row footer: inline links on one side, the copyright line on the
// other. It accepts either a flat list of links or the classic multi-column
// shape (the columns are flattened), so the footer config stays standard.
type FooterItem = Parameters<typeof FooterLinkItem>[0]['item'];

function flatten(links: unknown[]): FooterItem[] {
  return links.flatMap((entry) => {
    const column = entry as {items?: FooterItem[]};
    return column.items ?? [entry as FooterItem];
  });
}

const statusLabel: Record<ServiceStatus, string> = {
  checking: 'Checking system',
  up: 'All systems operational',
  down: 'System offline',
  unknown: '',
};

function Footer(): ReactNode {
  const {footer} = useThemeConfig();
  const status = useServiceStatus();
  if (!footer) {
    return null;
  }
  const items = flatten(footer.links ?? []);

  return (
    <footer className={clsx('footer', styles.footer)}>
      <div className={clsx('container', styles.row)}>
        {items.length > 0 && (
          <nav className={styles.links} aria-label="Footer">
            {items.map((item, i) => (
              <FooterLinkItem key={i} item={item} />
            ))}
          </nav>
        )}
        <div className={styles.end}>
          {status !== 'unknown' && (
            <span className={styles.status} role="status">
              <span className={clsx(styles.dot, styles[status])} aria-hidden="true" />
              {statusLabel[status]}
            </span>
          )}
          {footer.copyright && <p className={styles.copyright}>{footer.copyright}</p>}
        </div>
      </div>
    </footer>
  );
}

export default React.memo(Footer);
