#include "sdcard.h"
#include <M5Unified.h>
#include <SD.h>
#include <SPI.h>
#include <sd_diskio.h>
#include <ff.h>
#include <diskio_impl.h>

// Core2: o cartão fica no SPI da tela (SCLK 18, MOSI 23, MISO 38), que o
// M5GFX já iniciou no objeto SPI do Arduino
static constexpr uint8_t SD_CS = 4;
static constexpr uint32_t SD_HZ = 25000000;
static const char* const OBAS_DIR = "/obas";

static SdState state = SdState::None;

// O SD.begin só diz que falhou. Montar o FatFs direto (sem o VFS) mostra o
// motivo: FR_NO_FILESYSTEM é cartão que respondeu mas não tem FAT (exFAT,
// ext4, nunca formatado...).
static FRESULT probe() {
  uint8_t pdrv = sdcard_init(SD_CS, &SPI, SD_HZ);
  if (pdrv == 0xFF) return FR_INT_ERR;
  char drv[3] = {(char)('0' + pdrv), ':', 0};
  FATFS* fs = (FATFS*)calloc(1, sizeof(FATFS));
  FRESULT r = fs ? f_mount(fs, drv, 1) : FR_NOT_ENOUGH_CORE;
  f_mount(nullptr, drv, 0);
  free(fs);
  sdcard_uninit(pdrv);
  return r;
}

// Do sd_diskio do Arduino: existem, mas não estão no sd_diskio.h
DSTATUS ff_sd_initialize(uint8_t pdrv);
DSTATUS ff_sd_status(uint8_t pdrv);
DRESULT ff_sd_write(uint8_t pdrv, const uint8_t* buffer, DWORD sector, UINT count);
DRESULT ff_sd_ioctl(uint8_t pdrv, uint8_t cmd, void* buff);

// Uma leitura de vários setores (CMD18, que termina com CMD12) seguida de
// outra leitura às vezes trava o cartão: ele aceita o comando seguinte e
// nunca manda os dados, e só volta desligando a alimentação. Setor a setor
// (CMD17) isso não acontece.
static DRESULT readSectors(uint8_t pdrv, uint8_t* buf, DWORD sector, UINT count) {
  for (UINT i = 0; i < count; ++i) {
    if (!sd_read_raw(pdrv, buf + i * 512, sector + i)) return RES_ERROR;
  }
  return RES_OK;
}

static void readSectorBySector() {
  static const ff_diskio_impl_t impl = {ff_sd_initialize, ff_sd_status, readSectors, ff_sd_write, ff_sd_ioctl};
  for (uint8_t p = 0; p < FF_VOLUMES; ++p) {
    if (sdcard_type(p) != CARD_NONE) ff_diskio_register(p, &impl);
  }
}

static bool mount(bool format) {
  if (!SD.begin(SD_CS, SPI, SD_HZ, "/sd", 5, format)) return false;
  readSectorBySector();
  if (!SD.exists(OBAS_DIR)) SD.mkdir(OBAS_DIR);
  File root = SD.open("/");
  String names;
  int n = 0;
  for (File f = root.openNextFile(); f; f = root.openNextFile(), ++n) {
    if (n < 6) names += String(n ? ", " : "") + f.name() + (f.isDirectory() ? "/" : "");
  }
  Serial.printf("[sd] cartão %s de %llu MB, %llu MB usados; na raiz (%d): %s\n",
                SD.cardType() == CARD_SDHC ? "SDHC" : "SD", SD.cardSize() >> 20, SD.usedBytes() >> 20, n,
                names.c_str());
  return true;
}

// Cartão travado: responde aos comandos, mas a leitura dá erro, e reiniciar o
// ESP32 não resolve. Só desligando a alimentação dele, que no Core2 é o LDO2
// do AXP192. A tela passa por isso sem perder o estado (conferido lendo os
// registros dela antes e depois).
static bool powerCycle() {
  if (M5.getBoard() != m5::board_t::board_M5StackCore2 || M5.Power.getType() != m5::Power_Class::pmic_axp192) {
    return false;
  }
  M5.Power.Axp192.setLDO2(0);
  delay(300);
  M5.Power.Axp192.setLDO2(3300);
  delay(50);
  return true;
}

SdState sdBegin() {
  SD.end();  // o SD.begin não faz nada com o cartão já montado
  if (mount(false)) return state = SdState::Ready;
  FRESULT r = probe();
  if (r == FR_DISK_ERR && powerCycle()) {
    Serial.println("[sd] cartão travado: desliguei e liguei a alimentação dele");
    if (mount(false)) return state = SdState::Ready;
    r = probe();
  }
  state = r == FR_NO_FILESYSTEM ? SdState::Unformatted : SdState::None;
  Serial.printf("[sd] %s (FatFs %d)\n", state == SdState::Unformatted ? "cartão sem FAT32" : "sem cartão", r);
  return state;
}

bool sdFormat() {
  if (state != SdState::Unformatted) return false;
  uint32_t t0 = millis();
  bool ok = mount(true);  // o SD.begin só formata quando não acha FAT
  Serial.printf("[sd] formatação %s em %u s\n", ok ? "ok" : "falhou", (unsigned)((millis() - t0) / 1000));
  if (ok) state = SdState::Ready;
  return ok;
}

SdState sdState() { return state; }
