import io
import math
import re

import discord
from discord.ext import commands
import wand
from wand import color, image

import colornames
import rolecache
import utils

MIN_LUMINANCE = utils.setting('COLORS_MIN_LUMINANCE', 0.15)
MAX_LUMINANCE = utils.setting('COLORS_MAX_LUMINANCE', 0.75)
LIMIT_PALETTE = utils.setting('COLORS_LIMIT_PALETTE', False)
ROLE_PREFIX   = utils.setting('COLORS_ROLE_PREFIX',   '')

LUMINANCE_RANGE = (MIN_LUMINANCE, MAX_LUMINANCE)
ROLE_REGEX = re.compile(re.escape(ROLE_PREFIX) + '(#[a-fA-F0-9]{6})')

def generate_swatch(color, w=200, h=30):
    """Produces a file-like object with solid-color image.
    """
    f = io.BytesIO()
    c = wand.color.Color(str(color))
    img = wand.image.Image(width=w, height=h, background=c)
    img.format = 'png'
    img.save(file=f)
    f.seek(0)
    return f

def quantize(x):
    """Quantizes a byte into one of 8 equally-spaced values.
    """
    top = x & (7 << 5)
    return top | top >> 3 | top >> 6

def rgb9(color):
    """Converts a Discord color to 3-bit color depth.
    """
    return from_rgb(quantize(color.r), quantize(color.g), quantize(color.b))

def hex2color(code):
    """Convert a code code to a Discord color.
    """
    if code.startswith('#'):
        code = code[1:]
    if len(code) == 3:
        code = ''.join(c + c for c in code)
    if len(code) != 6:
        raise ValueError('invalid color code')
    n = int(code, base=16)
    return discord.Color(n)

def from_rgb(r, g, b):
    """Converts RGB values in a range of 0 to 255 to a discord.Color.
    """
    r, g, b = [int(max(0, min(x, 255))) for x in (r, g, b)]
    return discord.Color(r << 16 | g << 8 | b)

def test_luminance(r, g, b):
    fixed = clamp_luminance(from_rgb(r, g, b)).to_tuple()
    srgb1 = [max(x, 0.5) / 255 for x in (r, g, b)]
    srgb2 = [max(x, 0.5) / 255 for x in fixed]
    return relative_luminance(srgb1), relative_luminance(srgb2), fixed

def relative_luminance(srgb):
    rp, gp, bp = [x / 12.92 if x < 0.03928 else ((x + 0.055) / 1.055) ** 2.4
                  for x in srgb]
    return 0.2126 * rp + 0.7152 * gp + 0.0722 * bp

def bisection_search(f, a, b, *, n=10):
    fa = f(a)
    fb = f(b)

    for i in range(n):
        m = (a + b) / 2
        fm = f(m)

        if fm != fa:
            b, fb = m, fm
        else:
            a, fa = m, fm

    return a if fa else b

def scale_color(a, rgb, *, clamp=None):
    if clamp:
        return tuple(min(clamp, a * x) for x in rgb)
    else:
        return tuple(a * x for x in rgb)

def clamp_luminance(color, *, luminance_range=LUMINANCE_RANGE):
    """Brightens dark colors to at least a given minimum luma.
    """
    rgb = color.to_tuple()
    min_L, max_L = luminance_range

    srgb = [max(x, 0.5) / 255 for x in rgb]
    L = relative_luminance(srgb)

    if L < min_L:
        f = lambda a: relative_luminance(scale_color(a, srgb, clamp=1)) >= min_L
        a = bisection_search(f, 1, 255)
        return from_rgb(*scale_color(a * 255, srgb))
    elif L > max_L:
        f = lambda a: relative_luminance(scale_color(a, srgb, clamp=1)) <= max_L
        a = bisection_search(f, 0, 1)
        return from_rgb(*scale_color(a * 255, srgb))
    else:
        return color

_hexcolor = re.compile('#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})')
def get_color_info(color):
    """Looks up a color by hex code, exact name, or approximate name.

    Returns a pair of (discord color, list of color names). The
    first component is None if the color is malformed or
    ambiguous.
    """
    if _hexcolor.fullmatch(color):
        return hex2color(color), []

    exact = colornames.find_exact(color)
    if exact:
        code, canonical = exact
        return hex2color(code), [canonical]

    candidates = colornames.disambiguate(color)
    if len(candidates) == 1:
        best, = candidates
        code, canonical = colornames.find_exact(best)
        return hex2color(code), [canonical]

    return None, candidates

def sanitize_markdown(s):
    desparkled = re.sub(r'[*_`]', '', s)
    despaced = re.sub(r'[ \t\n\r]+', ' ', desparkled)
    return despaced

class Colors(rolecache.RoleCache):
    """Commands to let users assign themselves name colors.

    The roles generated by this module take the form of hexadecimal
    color codes (#XXXXXX) and can be shared between multiple users.
    """

    def adminhelp(self, ctx):
        desc = ("This module lets users assign themselves colors. Currently there are {} "
                "color roles in existence. If this seems too high, you can get rid of roles "
                "nobody is using with the `purgecolors` command.").format(
                    len(list(self.all_roles(ctx.message.server)))
                )
        return desc

    def key_for_role(self, role):
        m = ROLE_REGEX.fullmatch(role.name)
        if m:
            return m.group(1).lower()

    def is_color_role(self, role):
        return ROLE_REGEX.fullmatch(role.name)

    async def role_for_color(self, server, color):
        role = self.get_role(server, str(color))
        if role is None:
            name = ROLE_PREFIX + str(color)
            role = await self.bot.create_role(server, name=name, color=color, position=1)
        return role

    async def set_color(self, member, server, color):
        old_roles = list(filter(self.is_color_role, member.roles))
        if color is None:
            await self.bot.remove_roles(member, *old_roles)
        else:
            new_role = await self.role_for_color(server, color)
            old_role_ids = set(r.id for r in old_roles)
            updated_roles = [r for r in member.roles if r.id not in old_role_ids] + [new_role]
            await self.bot.replace_roles(member, *updated_roles)

    @commands.command(pass_context=True)
    async def swatch(self, ctx, *, color : str):
        """Show a sample swatch of a color.
        """
        color = sanitize_markdown(color)
        desired_color, color_names = get_color_info(color)
        if not desired_color:
            if color_names:
                await self.say_color_ambiguous(ctx, color, color_names)
            else:
                await self.say_color_unknown(ctx, color)
            return

        name = color_names[0] if color_names else None
        await self.send_swatch(ctx.message.channel, color=desired_color, name=name)

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    async def color(self, ctx, *, color : str):
        """Changes your name color.

        This command will accept a hexadecimal color code (like
        "#F0000D" or "#413") or a color name (like "violet" or
        "tangerine yellow"). Special thanks to Wikipedia for its list
        of 1300+ color names.
        """
        color = sanitize_markdown(color)
        desired_color, color_names = get_color_info(color)

        if not desired_color:
            if color_names:
                await self.say_color_ambiguous(ctx, color, color_names)
            else:
                await self.say_color_unknown(ctx, color)
            return

        canonical_name = color_names[0] if color_names else None

        effective_color = desired_color
        if LIMIT_PALETTE:
            effective_color = rgb9(effective_color)
        effective_color = clamp_luminance(effective_color, luminance_range=LUMINANCE_RANGE)

        await self.set_color(ctx.message.author, ctx.message.server, effective_color)

        mention = ctx.message.author.mention
        if effective_color == desired_color:
            message = "{} Here's your new color.".format(mention)
        else:
            message = "{} Here's the closest color to {} I can give you.".format(mention, desired_color)

        await self.send_swatch(ctx.message.channel, color=effective_color, name=canonical_name, content=message)

    @color.command(pass_context=True, no_pm=True)
    async def reset(self, ctx):
        """Resets your name color to the default.
        """
        await self.set_color(ctx.message.author, ctx.message.server, None)
        await self.say_color_removed(ctx)

    @commands.command(pass_context=True, no_pm=True)
    @commands.has_permissions(administrator=True)
    async def purgecolors(self, ctx):
        """Removes unused color roles (admin only).

        Users who change their name color frequently can leave behind
        extra color roles cluttering the roles list. This command
        cleans them up without impacting colors which are in use. The
        process may take several seconds to complete.
        """
        server = ctx.message.server
        await self.bot.send_typing(ctx.message.channel)

        color_roles = list(self.all_roles(server))
        used_roles = set(role.id
                         for member in server.members
                         for role in member.roles)
        deleted = 0
        for role in color_roles:
            if role.id not in used_roles:
                await self.bot.delete_role(server, role)
                deleted += 1

        await self.bot.reply("Removed {} unused color roles.".format(deleted))

    async def send_swatch(self, channel, color, content=None, name=None):
        if name:
            title = '{} - {}'.format(str(color), name)
        else:
            title = str(color)
        with generate_swatch(color) as f:
            name = '{}.png'.format(str(color).replace('#', ''))
            em = discord.Embed(title=title)
            em.set_image(url='attachment://{}'.format(name))
            return await self.bot.send_file(channel, f, content=content, embed=em, filename=name)

    async def say_color_unknown(self, ctx, color_name):
        if color_name.startswith('#'):
            message = "Are you sure that's a real hex code?"
        else:
            message = "Sorry, I don't know what color \"{}\" is.".format(color_name)
        await self.bot.reply(message)

    async def say_color_ambiguous(self, ctx, color_name, candidates):
        num_best = 10
        best = list(sorted(candidates, key=lambda c: (len(c), c)))[:num_best]
        if len(candidates) > num_best:
            more = ' , ... [{} more candidates]'.format(len(candidates) - num_best)
        else:
            more = ''
        options = ', '.join('**' + c + '**' for c in best)
        message = "I'm not sure what color you wanted. Maybe try one of these: {}{}.".format(options, more)
        await self.bot.reply(message)

    async def say_color_removed(self, ctx):
        message = "Welcome to the ＣＯＬＯＲ ＶＯＩＤ.".format(
            ctx.message.author.display_name)
        await self.bot.reply(message)
