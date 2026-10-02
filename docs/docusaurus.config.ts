import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const repoUrl = 'https://github.com/illinoisdata/readie';

const config: Config = {
  title: 'Readie',
  tagline: 'Checkpoint and restore for serverless Python.',
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
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        indexBlog: false,
        docsRouteBasePath: '/docs',
        highlightSearchTermsOnTargetPage: true,
      },
    ],
  ],

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
        {type: 'docSidebar', sidebarId: 'gettingStarted', position: 'left', label: 'Get started'},
        {type: 'docSidebar', sidebarId: 'concepts', position: 'left', label: 'Concepts'},
        {type: 'docSidebar', sidebarId: 'architecture', position: 'left', label: 'Architecture'},
        {type: 'docSidebar', sidebarId: 'guides', position: 'left', label: 'Guides'},
        {type: 'docSidebar', sidebarId: 'reference', position: 'left', label: 'Reference'},
        {type: 'docSidebar', sidebarId: 'contributing', position: 'left', label: 'Contribute'},
        {href: repoUrl, label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Learn',
          items: [
            {label: 'Quickstart', to: '/docs/getting-started/quickstart'},
            {label: 'How it works', to: '/docs/architecture/overview'},
            {label: 'Glossary', to: '/docs/guides/glossary'},
          ],
        },
        {
          title: 'Build with Readie',
          items: [
            {label: 'Using @remote', to: '/docs/reference/sdk'},
            {label: 'Troubleshooting', to: '/docs/guides/troubleshooting'},
            {label: 'Contribute', to: '/docs/contributing/setup'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'GitHub', href: repoUrl},
            {label: 'Report an issue', href: `${repoUrl}/issues`},
            {label: 'Security policy', to: '/docs/architecture/security'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} The Readie authors. Apache-2.0 licensed. Built with Docusaurus.`,
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'go', 'protobuf', 'toml', 'yaml', 'docker', 'nginx'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
