---
name: tailor-resume
description: Tailor base.yaml resume to a job description provided by the user
---

# Tailor Resume to Job Description

Given a job description provided by the user, tailor `base.yaml` to maximize relevance for that specific role. The resume must remain on **one page** after all changes.

## Where each tailored version lives

Every job gets its own branch and its own git worktree. `main` holds the general-purpose resume and is never edited by this skill.

- **Branch name:** `tailor/<company>-<role>`, lowercase, words joined with hyphens, kept short (e.g. `tailor/stripe-ml-infra-new-grad`, `tailor/imc-swe-intern`). If the branch already exists, ask the user whether to update it or pick a new name (append `-2`, `-3`, ...).
- **Worktree:** `.worktrees/<company>-<role>` at the repo root (gitignored). Create both in one go from an up-to-date `main`:
  ```bash
  git fetch origin main
  git worktree add -b tailor/<company>-<role> .worktrees/<company>-<role> origin/main
  ```
- Do all edits, renders, and commits for the job inside that worktree.
- **Job record:** commit these files in the worktree alongside `base.yaml` and the rendered PDF, so the branch is the complete record for that application:
  - `job/meta.json`: `{"company": ..., "role": ..., "url": <posting link or null>, "tailored_on": "YYYY-MM-DD", "base_commit": <main commit the branch started from>}`
  - `job/posting.md`: the full job description as read (source link on the first line), unedited.
  - `job/changes.md`: what changed and why, one short section per step below (skills, experience, projects, coursework, bold keywords, page fit).
- **Save:** commit with message `Tailor resume for <Company> <Role>` and push the branch (`git push -u origin tailor/<company>-<role>`). Never merge it into `main`.
- When done with a job, the worktree can be removed (`git worktree remove .worktrees/<company>-<role>`); the branch keeps everything.

## Steps

0. **Set up the branch and worktree** for this job as described above, and save the job description to `job/posting.md`.

1. **Read `base.yaml`** to understand the current state of all sections, including commented-out entries.

2. **Analyze the job description** for:
   - Key technologies, languages, and frameworks mentioned
   - Desired experience areas (ML, backend, frontend, infra, etc.)
   - Soft skills and themes (collaboration, experimentation, prototyping, etc.)

3. **Reorder skills** so the most relevant languages and tools appear first. Add or remove items to match what the role values. Keep the two-row format (Languages / Libraries and Tools).

4. **Adjust experience bullet points** to emphasize the aspects of each role most relevant to the target job. Reframe language (e.g., "AI deployments" → "ML model deployments" for an ML role) without fabricating accomplishments. Preserve quantitative metrics.

5. **Swap projects**: Comment out less-relevant active projects and uncomment ones that better match the role. All projects (active and commented-out) in the file are fair game.

6. **Update coursework**: Add or remove courses from the coursework list to reflect relevance.

7. **Update `bold_keywords`**: Replace the list with terms that actually appear in the active (uncommented) resume content and are relevant to the target role. Remove keywords for commented-out content.

8. **Verify one-page fit and fill**: Render the resume by running `uvx 'rendercv[full]@2.8' render base.yaml` (same version GitHub renders with), then visually inspect the resulting PDF (named by `settings.render_command.pdf_path`). The resume must fit on one page but also fill the page with minimal empty space at the bottom. If it overflows, trim the least-relevant bullets (prefer trimming from projects or older experience roles). If there is significant empty space, uncomment or add relevant bullets until the page is filled. Do **not** adjust the `design` section margins/spacing to force a fit.

9. **Record and save**: write `job/meta.json` and `job/changes.md`, then commit and push the branch as described above.

## Constraints

- Do not invent or exaggerate accomplishments — only reframe existing ones.
- Do not modify the `design`, `locale`, or `settings.render_command` sections.
- Keep entries in reverse-chronological order within each section.
- Commented-out entries should use the existing `#` comment style, indented to match their block.
- Avoid single-word line overflows in bullet points. If a bullet wraps to a second line, either shorten it to fit on one line or extend it so the second line reaches at least halfway across the page.
