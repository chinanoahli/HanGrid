"""Portable output names, separate from installed font identities."""
import re


def filename(identity, extension, nerd=None):
    family = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', identity['family_base']).strip(' .')
    family = family.replace(' ', '')[:100] or 'HanGridMono'
    # Keep the complete filename below common 255-byte filesystem limits.
    while len(family.encode('utf-8')) > 120:
        family = family[:-1]
    style = identity['weight'] + ('Italic' if identity.get('italic') else '')
    regions = ('CJK' if identity['coverage'] == 'pan-cjk' else
               '+'.join(region.upper() for region in identity['languages']))
    parts = [family, style, regions]
    if identity.get('windows_trim'):
        parts.append('WinTrim')
    if identity.get('nerd') if nerd is None else nerd:
        parts.append('NF')
    return '-'.join(parts) + '.' + extension.lstrip('.')
