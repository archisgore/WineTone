# Finally, wine recommendations that speak your language

*Posted 2026-05-22*

You have been to the wine store and read the shelf-talker that
promises "elegant, with notes of cassis and tobacco." You have
brought the bottle home and tasted, instead, something closer to
grape juice that visited a campfire. You liked it, and you would
like more bottles in that vicinity, but "campfire grape juice" is
not a category on any recommendation app you have ever used.

That is the gap WineTone exists to close.

## Why most wine recommenders fail you

Apps like Vivino match you to wines that *other people* described
in a way the system already understands. When you share the same
vocabulary as those other people, this works reasonably well. When
you do not — when "grippy" means tannic structure to one drinker
and astringency to another, when your "oaky" is reaching for
tandoor smoke rather than bourbon barrels — the recommendations
drift further from what you actually want with every search you
make.

You do not have bad taste. You have *your own* taste, and nobody
has bothered to listen to it.

## How WineTone works

1. **Sign in and pick a username.** You can sign in with email,
   Google, or GitHub. It takes only a moment. A pseudonym is fine
   and is actually preferred, since we would rather not know your
   real name, and we do not ask for a phone number or an address.
2. **Label five wines you know.** Search the catalog for a wine
   you have tried and describe what it tastes like to you. The
   description does not have to read the way a sommelier would
   write it; the words that come to you naturally are the ones the
   system needs. Examples might include "like the rain after a hot
   day," "tastes purple," or "reminds me of my grandmother's
   pantry." Honesty is the only requirement.
3. **WineTone calibrates.** Your descriptions become a map between
   the words you use and the catalog of 164,000 wines. The system
   learns that when *you* say "grippy," you mean a particular
   neighborhood of wines, which may be a different neighborhood
   from the one someone else calls "grippy."
4. **Ask for something.** Type whatever you are in the mood for,
   in your own words, and the recommendations come back tuned to
   your vocabulary rather than to the average reviewer's.

The whole process takes about three minutes.

## Your words do not have to be "wine words"

The most distinctive thing about WineTone is that the vocabulary
you teach it does not have to be about taste or aroma at all.

Suppose you opened a Riesling for your sister's birthday — bright,
slightly sweet, the kind of bottle that made the room a little
louder when you poured it. You can simply label that Riesling your
"birthday wine." WineTone will work out what about that wine makes
it a birthday wine to you — the acidity, the residual sugar, the
off-dry brightness, whatever the hidden pattern turns out to be —
and the next time you ask for something for a birthday, it will
find other wines that live in the same neighborhood.

The vocabulary can be about context, mood, occasion, or memory.
A few examples to anchor the idea:

- *"Morning wine"* is what you reach for at brunch on a slow Saturday.
- *"Evening wine"* is what closes the day.
- *"Weekend wine"* is what shows up when you have time to actually
  taste what is in the glass.
- *"Melancholy wine"* is what you pour when you are sitting with
  something heavy.
- *"Pizza wine"* is exactly what it sounds like.
- *"The wine my dad would pour"* is its own kind of category.

Each of those labels becomes a coordinate in the same space the
system uses for "tannic" or "high acid." You name the dimensions;
WineTone learns the geometry. The list of useful labels is
effectively endless, because *your* axes of taste are.

## What this unlocks

**Recommendations that match your actual palate.** Tell WineTone
that you want something soft and forgiving for a Tuesday, and it
will work out what that phrase means to you and recommend
accordingly, rather than handing you the wine that the average
reviewer happened to describe with similar words. Each suggestion
arrives with a one-sentence explanation, grounded in your own
labels, so you understand why the system surfaced what it did.

**A label-scanner you can use at the store.** When you are
standing in front of a confusing wall of bottles, open WineTone on
your phone, tap the camera button, and take a photo of any label.
The system will tell you what the wine is, what other people have
said about it, and whether it aligns with the palate you have
already taught it.

**A shareable palate identity card.** Every signed-in user has a
public summary page at `/u/{username}/palate`, which condenses your
calibration into five interpretable sliders (savoury ↔ fruity,
structured ↔ soft, old world ↔ new world, light ↔ bold, dry ↔
off-dry), a list of the descriptors you reach for more often than
average, and a few of your recent labels. It is the closest thing
the project has to a profile picture — a fingerprint of your taste
rather than your face. Share it with friends so you can compare
where their palates and yours diverge.

**Less friction on the first day.** If labelling five wines from a
blank slate feels like work, the onboarding flow at `/onboarding`
lets you pick one of three palate archetypes — an old-world
structurer, a new-world fruit-lover, or a natural-wine adventurer —
and the dashboard then suggests five catalog wines that fit the
archetype you chose. Label any of them in your own words and the
system has a starting point that is not a blank slate.

**Editable labels.** If you change your mind about a wine, the
edit link next to each entry on your dashboard opens an inline
form. Update the description, switch the sentiment, save. Your
projection re-fits the next time you ask it to.

**A way to find people who taste like you.** A public directory
shows everyone using WineTone along with their labels, their
positive and negative reactions, and their calibration status. If
you follow the users whose words sound most like yours, their
calibrations help refine your own. Two follows plus two of your
own labels can be enough to start getting useful recommendations.

**Room to say what you do not want.** Most wine apps only listen
for "I love this." WineTone lets you record the opposite — "I
described this wine, and I would like the system to give me fewer
things like it" — with a single thumbs-down. The model treats this
as real information and learns to push away from those wines
rather than toward them.

**An installable home-screen icon.** WineTone is a web app, but on
iPhone (via Safari → Share → Add to Home Screen) and Android
(Chrome will prompt you automatically) it installs to the home
screen like any other app. Tap the icon and the site opens
full-screen, with no browser chrome and no App Store in the way.

## What it costs

WineTone is free, open source, and runs on a single developer's
hobby budget. The closest thing to a catch is the privacy policy,
which can be summarized in a sentence: **everything you type is
public.** If you choose a pseudonym and avoid entering anything
you would not be comfortable posting publicly elsewhere, you have
nothing to worry about. There is no tracking, no email marketing,
and no premium tier hiding behind the recommendations.

## Try it

Visit [**tone.wine**](https://tone.wine), bring five wines you
know, and describe them honestly. See what the recommendations
look like once the system has learned to listen the way you
actually speak.
