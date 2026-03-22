-- conditional.lua: filter out Div blocks based on a 'section' metadata variable.
-- Usage: pandoc --metadata section=cs429 -L filters/conditional.lua ...
--
-- Blocks tagged with .only-cs429 are kept only when section=cs429,
-- blocks tagged with .only-cs629 are kept only when section=cs629, etc.
--
-- Two passes are required: the first reads Meta (so the section variable is
-- set before any Div elements are visited in the second pass).

local section = ""

return {
  {
    Meta = function(m)
      if m.section then
        section = pandoc.utils.stringify(m.section)
      end
      return m
    end
  },
  {
    Div = function(el)
      for _, cls in ipairs(el.classes) do
        if cls:match("^only%-") then
          return cls == ("only-" .. section) and el.content or {}
        end
      end
    end
  }
}
