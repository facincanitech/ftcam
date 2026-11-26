"""Gera icon.png e icon.ico do FTCam."""
from PIL import Image, ImageDraw


def draw(size=512):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # fundo arredondado em degradê azul
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s
        gd.line([(0, y), (s, y)], fill=(int(40 + 20 * t), int(120 - 40 * t), int(240 - 60 * t), 255))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    # corpo da câmera
    w = s * 0.08
    d.rounded_rectangle([s * .16, s * .30, s * .66, s * .70], radius=int(s * .08), outline="white", width=int(w))
    d.polygon([(s * .70, s * .44), (s * .86, s * .33), (s * .86, s * .67), (s * .70, s * .56)], fill="white")
    # ponto de gravação
    r = s * .07
    d.ellipse([s * .41 - r, s * .50 - r, s * .41 + r, s * .50 + r], fill=(255, 77, 79, 255))
    return img


if __name__ == "__main__":
    im = draw()
    im.save("icon.png")
    im.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("ícones gerados")
