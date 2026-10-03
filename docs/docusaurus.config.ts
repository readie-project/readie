import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const repoUrl = 'https://github.com/illinoisdata/readie';

const config: Config = {
  title: 'Readie',
  tagline: 'Rapid code execution for Python via adaptive checkpointed environments',
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

  organizationName: 'illinoisdata',
  projectName: 'readie',

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
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Readie',
      logo: {
        alt: 'Readie logo',
        src: 'img/logo.svg',
      },
      items: [
        {type: 'docSidebar', sidebarId: 'gettingStarted', position: 'right', label: 'Get started'},
        {type: 'docSidebar', sidebarId: 'guides', position: 'right', label: 'Guides'},
        {type: 'docSidebar', sidebarId: 'concepts', position: 'right', label: 'Concepts'},
        {type: 'docSidebar', sidebarId: 'architecture', position: 'right', label: 'Architecture'},
        {type: 'docSidebar', sidebarId: 'contributing', position: 'right', label: 'Contribute'},
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
        {label: 'SDK reference', to: '/docs/guides/sdk'},
        {label: 'Contribute', to: '/docs/contributing/setup'},
        {label: 'Security', to: '/docs/architecture/security'},
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
