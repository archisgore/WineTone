# WineTone was never about Wine

## It's an experiment in Hyper-Linguistics

**By Archis Gore | May, 2026**

*Originally published on Medium: <https://medium.com/@archisgore/winetone-was-never-about-wine-99db4d4cb288>*

---

If the same alphabet isn't pronounced the same in different languages, and if the same word doesn't mean the same in different languages (i.e. ask for some Pain in an English vs French), then it should be easy to believe that two English speakers saying the exact same thing, could *mean something entirely different*.

The real facade being that English is any way **a common language** we speak. In reality we all speak English-ish — a custom language with personal semantics, using common symbology at various layers — at the base layer the script, at the next layer the spellings, and the layer above, grammar.

I'm going to call this personal language a **HyperLanguage** to differentiate it from the common symbology of broad languages like English, Russian, or Urdu.

What we convey using those symbols is as different as a person named Jesus being a sole individual in the past 2000 years for an English speaker, and the guy next door for a Spaniard or Latin American.

The real tragedy of LLMs isn't that they're doing too much, it's that the moment we found the 2020's version of Eliza with decoder-only models, we stopped utilizing half the power of a complete Transformer architecture.

## The problem Transformers were built to solve…

> **"Communication is the process of transferring an AST from one individual to another."**

Lera Boroditsky's many talks explain the challenge of conveying meaning across languages.

This is well known. As a small example to set context, I come from a culture with large patriarchal joint families — dozens of people across 3+ generations living under one roof. We have nuanced words aunts, uncles and cousins that place them within 4 hops of my family tree conveying in a single word: fathers or mothers side, male or female individual, the generation upto +2 in either direction, and gender and relationship of lineage, i.e. son of brother of my mother vs daugher of sister of my father. The only English equivalents to these are: grandparent, aunt, uncle, and cousin.

So how do you translate full texts across languages? This is what the Encoder half of a Transformer does (your ChatGPT/Claude/Codex/Grok are all Decoder-only models).

In simple terms, lets represent each individual as a set of values each representing one degree of freedom: [generation-1, fathers side, male, husband-of-fathers-sister, etc.]

If we can fill these in based on context, then the phrase: "male cousin on mother's side once-removed" becomes: "mame-bhau" in Marathi and vice-versa.

In short, this list of values is an **embedding**. The more dimensions (aka degrees-of-freedom) we provide an embedding, the more nuance it can hold.

If we get this far with capturing this nuance… what if we treated two English-speakers as speaking HE1 (Hyper-English-1) and HE2 (Hyper-English-2)? Have you ever had a conversation with an individual from the exact same city, town, education, cultural background, generation and yet felt like they "*just weren't getting it*"? It's been my entire lived experience. Wouldn't it be nice if we could translate HE1 -> HE2 and convey our precise meaning?

On the other side of Computer Science is something called an Abstract Syntax Tree (AST). It is a normalized, canonical, uniform way to represent semantics accurately and precisely. When two ASTs are equal, they mean precisely the same thing.

In short: **"Communication is the process of transferring an AST from one individual to another."**

## We just didn't take Transformers far enough

Imagine if we continued to build encoders (I'll call them Projections for now) from HyperEnglish1 into the English or more general Embedding Space? Could we capture our meaning better such that a reader could decode our intent in a way they understood in their HyperEnglish2?

That is what WineTone is really about. This is why WineTone itself is open source, not really a business for me, but a playground for my intellectual exercise of HyperLinguistics.

In my opinion, even the best AI-powered therapy, counselling, depression, relationship advice, coaching, etc. apps being powered by (aka wrappers over) Decoder-only LLMs are missing the deeper power transformers give them.

Imagine an app where cultural nuances of phrasing are translated across boundaries. Relationship counselling where true intent is discovered, embedded and then translated for the recipient. In short, a world where we can accurately project our AST to others every single time. A world of **HyperLinguistics**!

It's just a lot more fun to do this while drinking wine!

## How WineTone works

Let's assume that the identity of "A Wine" is closed over it's bottle. I mean identity in a real Ship-of-Theseus kind of way. What "a wine is" in the most philosophical, moral, ethical, physical, literal, colloquial way. What I mean by closed is that what make the wine itself, is all contained — the molecules, the mixture (solution, emulsion, etc.), the proportions, the chirality, and so on.

Which is to say that "A Wine" is a very definable, finite, fixed entity. Which is to say that descriptions of said wine are **subjectively objective —** they are subjective to the person coming up with them, but they represent a very objective fixed entity.

Let's place a wine on a 2-dimensional embedding space. One dimension will be sweetness, and the other dimension will be… sweetness?

Let's project 4 wines on this graph. x-dimension is how sweet a wine is from 0 to 100. y-dimension captures the "kind of sweetness" — surely we all know that not all sweet things are the same. Aspertame, stevia, fructose and sucrose all taste different. Sweetness can be a pure smell — like that of honey. Sweetness could take up easily 100 dimensions in our embedding.

How do wine experts define it? They use eigenvectors of course, they just don't know it. When they say a wine has hints of honeysuckle, or grapefruit, or oak — they're collapsing thousands of dimensions into one dimension and pivoting around that dimension. Grapefruit covers all the layers of citrus, layer of bitterness, and more. Subtract the grapefruit and whatever is left is given a name. I'd be surprised if they have specific verbiage for the dozens of kinds of sweetness.

Sommaliers basically have two things that are going for them: Consensus and Ceremony. Consensus is easy — everyone uses the same basis vectors they learn through incessant practice. No different than learning to play an instrument. And Ceremony is when you create a bit of aura around a title and continually remind people of the separation between you and them.

Sure there are a few naturally gifted people — but there can only be so many Mozarts' and Johns' Williams.

Practically this means that there can be thousands of basis vectors to choose from that describes the same wine. And we have the perfect mechanism to convert anyone's arbitrary description into this vector space: An Attention Encoder!

And this is exactly what WineTone does:

1. For each wine it produces a self-learned embedding space. It doesn't matter what the space is. It'll have a bunch of basis vectors and each wine will be some combination of those bases.
2. Every individual description provided for a wine is projected onto this embedding space, for now with a simple linear product. It proves the point. In short, I'm simply manipulating the embedding space to my basis vectors.
3. Then in my transformed space, I can label wines how I want. If my "sweetness" tends to be more aspartame, then the shift of origin + scaling of the y-axes puts all the aspartame-y wines right next to where my sweetness cluster search happens.

That's basically it. In the future I'd love to build attention layers, but it really isn't needed. At some point I plan to use chemical analyses to add some fixed objective dimensions to the embeddings. But they're not strictly needed.

WineTone is a language translater between your language and everyone elses.

## The point is larger than Wine

If you're an AI researcher, product developer or especially an anthropologist, linguist or social scientist, consider the possibilities of capturing, translating and conveying accurate intent. How many relationships could be saved, how many egos could be not bruised, how many wars might be avoided?

We have the tool, and we're obsessed with a chatbot.
