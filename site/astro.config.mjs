// @ts-check
import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://asimoov.com',
  base: '/',
  trailingSlash: 'ignore',
  integrations: [
    starlight({
      title: 'ASIMOOV',
      description: 'The open-source soul for companion robots.',
      customCss: ['./src/styles/theme.css', './src/styles/starlight.css'],
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/florent31000/asimoov' },
      ],
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        fr: { label: 'Français', lang: 'fr' },
      },
      sidebar: [
        {
          label: 'Documentation',
          translations: { fr: 'Documentation' },
          items: [
            { slug: 'docs/getting-started' },
            { slug: 'docs/concepts' },
            { slug: 'docs/bodies' },
            { slug: 'docs/contracts' },
            { slug: 'docs/privacy' },
            { slug: 'docs/costs' },
          ],
        },
      ],
      components: {
        SiteTitle: './src/components/SiteTitle.astro',
      },
      editLink: {
        baseUrl: 'https://github.com/florent31000/asimoov/edit/main/site/',
      },
      favicon: '/favicon.svg',
      lastUpdated: false,
      pagination: false,
    }),
  ],
});
