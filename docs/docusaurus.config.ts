import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const repoUrl = 'https://github.com/readie-project/readie';

// The tagline, which is also the description of every page that sets none of its own (each doc
// sets one in its front matter). The landing page shows it under the title, and link previews
// of the landing page show it as their description.
const siteDescription =
  'Secure sandboxes with optimized startup and import times for AI agents and running Python code remotely.';

const config: Config = {
  title: 'Readie',
  tagline: siteDescription,
  favicon: 'img/favicon.ico',
  headTags: [
    // Browsers that support SVG icons use the logo directly; the .ico above is the fallback.
    {
      tagName: 'link',
      attributes: {rel: 'icon', type: 'image/svg+xml', href: '/img/logo.svg'},
    },
  ],

  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  url: 'https://readie.org',
  baseUrl: '/',
  trailingSlash: false,

  organizationName: 'readie-project',
  projectName: 'readie',

  // The playground service the playground on the landing page posts to. Empty disables the Run button.
  customFields: {
    playgroundUrl: process.env.PLAYGROUND_URL ?? '',
    repoUrl,
  },

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  themes: [
    '@docusaurus/theme-mermaid',
    // Search is switched off for now. To bring it back, uncomment this entry; the
    // package (@easyops-cn/docusaurus-search-local) is still installed.
    // [
    //   '@easyops-cn/docusaurus-search-local',
    //   {
    //     hashed: true,
    //     indexBlog: false,
    //     docsRouteBasePath: '/docs',
    //     highlightSearchTermsOnTargetPage: true,
    //   },
    // ],
  ],

  clientModules: ['./src/clientModules/navbarScroll.ts'],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: `${repoUrl}/edit/main/docs/`,
          showLastUpdateTime: true,
        },
        blog: false,
        // Every page is listed with the date its source last changed. The weekly change
        // frequency and the flat priority that Docusaurus writes by default say nothing to
        // search engines, so they are left out.
        sitemap: {
          lastmod: 'date',
          changefreq: null,
          priority: null,
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // The card that link previews show (Open Graph and Twitter). Its source is
    // tools/social-card.html.
    image: 'img/social-card.png',
    metadata: [
      {name: 'description', content: siteDescription},
      {property: 'og:site_name', content: 'Readie'},
      {property: 'og:type', content: 'website'},
      {property: 'og:image:width', content: '1200'},
      {property: 'og:image:height', content: '630'},
      {property: 'og:image:alt', content: `Readie: ${siteDescription}`},
      {name: 'twitter:image:alt', content: `Readie: ${siteDescription}`},
    ],
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      // The wordmark carries the name, so the navbar has no separate text title.
      logo: {
        alt: 'Readie',
        src: 'img/wordmark.svg',
        srcDark: 'img/wordmark-dark.svg',
      },
      items: [
        {type: 'docSidebar', sidebarId: 'gettingStarted', position: 'right', label: 'Get started'},
        {type: 'docSidebar', sidebarId: 'guides', position: 'right', label: 'Guides'},
        {type: 'docSidebar', sidebarId: 'concepts', position: 'right', label: 'Concepts'},
        {type: 'docSidebar', sidebarId: 'architecture', position: 'right', label: 'Architecture'},
        {type: 'docSidebar', sidebarId: 'contributing', position: 'right', label: 'Contribute'},
        // A section of the landing page, not a page: never shown as the current page.
        {to: '/#playground', label: 'Playground', position: 'right', activeBaseRegex: '^$'},
        {
          href: repoUrl,
          label: 'GitHub',
          position: 'right',
          className: 'navbar-github',
          'aria-label': 'Readie on GitHub',
        },
      ],
    },
    footer: {
      style: 'light',
      links: [
        {label: 'Quickstart', to: '/docs/getting-started/quickstart'},
        {label: 'Architecture', to: '/docs/architecture/overview'},
        {label: 'Playground', to: '/#playground'},
        {label: 'Contribute', to: '/docs/contributing/setup'},
        {label: 'Security', to: '/docs/architecture/security'},
        {label: 'PyPI', href: 'https://pypi.org/project/readie'},
        {label: 'GitHub', href: repoUrl},
      ],
      copyright: `Copyright © ${new Date().getFullYear()} The Readie authors. Apache-2.0 licensed.`,
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
    prism: {
      theme: prismThemes.oneLight,
      darkTheme: prismThemes.oneDark,
      additionalLanguages: ['bash', 'go', 'protobuf', 'toml', 'yaml', 'docker', 'nginx'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
